"""Multi-process acceptance, real crash rollback, and durable identity evaluation.

Run: python -m evals.module1 --rounds 10 --workers 4 --output artifacts/module1-evaluation.json
All databases/workspaces are temporary. No models, network or external effects.
"""

import argparse
import json
import multiprocessing
import os
import platform
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from weft.domain import RunState, StateConflict, SubmissionConflict, TaskSpec, operation_id
from weft.storage.migrations import transaction, utc_now
from weft.storage.sqlite import SQLiteExecutionRepository


def _initialize(barrier):
    global _barrier
    _barrier = barrier


def _race(root, key, index, action, run_id=None):
    repo = SQLiteExecutionRepository(Path(root))
    _barrier.wait(timeout=30)
    if action == "submit":
        submission = repo.submit("evaluation", TaskSpec("report", "isolated", {"budget": 10}), idempotency_key=key)
        return {"created": submission.created, "run_id": submission.run.run_id, "pid": os.getpid()}
    try:
        _, attempt = repo.start_attempt(run_id, 0, f"process:{os.getpid()}:{index}")
        return {"started": True, "attempt_id": attempt.attempt_id, "pid": os.getpid()}
    except StateConflict:
        return {"started": False, "pid": os.getpid()}


def _crash_before_commit(root, run_id):
    path = Path(root) / "executions.sqlite3"
    with transaction(path) as conn:
        conn.execute("INSERT INTO attempts VALUES ('uncommitted', ?, 1, 'crashing', 'running', ?, NULL)",
                     (run_id, utc_now()))
        conn.execute("UPDATE runs SET state='running', version=1, attempt_count=1, current_attempt_id='uncommitted' WHERE run_id=?",
                     (run_id,))
        # Deliberately kill only this evaluation child without context cleanup.
        os._exit(23)


def evaluate(rounds, workers):
    context = multiprocessing.get_context("spawn")
    started = time.perf_counter()
    counts = {"submission_single_winner": 0, "attempt_single_winner": 0,
              "conflicting_request_rejected": 0, "stale_version_rejected": 0,
              "pause_resume_identity_preserved": 0, "new_run_operation_identity_separated": 0,
              "crash_transaction_rolled_back": 0}
    timings = []
    process_ids = set()
    with tempfile.TemporaryDirectory(prefix="weft-module1-") as directory:
        repo = SQLiteExecutionRepository(Path(directory))
        barrier = context.Barrier(workers)
        with ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                 initializer=_initialize, initargs=(barrier,)) as pool:
            for number in range(rounds):
                tick = time.perf_counter()
                key = f"round:{number}"
                submissions = [f.result(timeout=60) for f in [
                    pool.submit(_race, directory, key, i, "submit") for i in range(workers)]]
                assert sum(s["created"] for s in submissions) == 1
                assert len({s["run_id"] for s in submissions}) == 1
                counts["submission_single_winner"] += 1
                run_id = submissions[0]["run_id"]
                attempts = [f.result(timeout=60) for f in [
                    pool.submit(_race, directory, key, i, "start", run_id) for i in range(workers)]]
                process_ids.update(a["pid"] for a in attempts)
                assert sum(a["started"] for a in attempts) == 1
                run = repo.get_run(run_id)
                assert run.attempt_count == 1 and len(repo.attempts(run_id)) == 1
                counts["attempt_single_winner"] += 1
                try:
                    repo.submit("evaluation", TaskSpec("DIFFERENT", "isolated"), idempotency_key=key)
                except SubmissionConflict:
                    counts["conflicting_request_rejected"] += 1
                else:
                    raise AssertionError("Conflicting request accepted")
                try:
                    repo.transition(run_id, 0, RunState.FAILED, attempt_id=run.current_attempt_id)
                except StateConflict:
                    counts["stale_version_rejected"] += 1
                else:
                    raise AssertionError("Stale writer accepted")
                requested = repo.transition(run_id, run.version, RunState.PAUSE_REQUESTED)
                paused = repo.transition(run_id, requested.version, RunState.PAUSED, attempt_id=run.current_attempt_id)
                queued = repo.transition(run_id, paused.version, RunState.QUEUED)
                resumed, attempt = repo.start_attempt(run_id, queued.version, "recovery-evaluation")
                assert resumed.run_id == run_id and attempt.number == 2 and attempt.attempt_id != run.current_attempt_id
                assert operation_id(run_id, "step:1") == operation_id(resumed.run_id, "step:1")
                counts["pause_resume_identity_preserved"] += 1
                repo.transition(run_id, resumed.version, RunState.SUCCEEDED, attempt_id=attempt.attempt_id)
                new = repo.rerun(run.task_id, run_id)
                assert operation_id(new.run_id, "step:1") != operation_id(run_id, "step:1")
                counts["new_run_operation_identity_separated"] += 1
                child = context.Process(target=_crash_before_commit, args=(directory, new.run_id))
                child.start()
                child.join(timeout=20)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
                    raise AssertionError("Crash scenario did not finish")
                assert child.exitcode == 23
                assert repo.get_run(new.run_id) == new and repo.attempts(new.run_id) == []
                counts["crash_transaction_rolled_back"] += 1
                timings.append(round(time.perf_counter() - tick, 4))
    assert len(process_ids) == workers
    return {"schema_version": 1, "module": "execution-foundations", "passed": True,
            "rounds": rounds, "workers": workers, "independent_worker_processes": len(process_ids),
            "model_calls": 0, "external_side_effects": 0,
            "checks": {name: {"passed": count, "total": rounds} for name, count in counts.items()},
            "total_checks": rounds * len(counts), "round_seconds": timings,
            "duration_seconds": round(time.perf_counter() - started, 4),
            "environment": {"python": platform.python_version(), "platform": platform.system()},
            "limits": ["No checkpoint replay", "No side-effect reconciliation", "No leases or worker takeover",
                       "Pause/resume evaluates repository identity, not agent continuation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("artifacts/module1-evaluation.json"))
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1000 or not 2 <= args.workers <= 32:
        parser.error("rounds must be 1..1000 and workers 2..32")
    report = evaluate(args.rounds, args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "total_checks": report["total_checks"],
                      "duration_seconds": report["duration_seconds"], "report": str(args.output)}))


if __name__ == "__main__":
    main()
