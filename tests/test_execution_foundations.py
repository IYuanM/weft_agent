"""Module 1 acceptance: state, storage, concurrency, migrations and CLI adapter."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from baseline.core.agent import Agent
from baseline.models.chat import Reply
from baseline.persistence.store import Store
from baseline.tools.executor import ToolExecutor
from baseline.tools.files import FileTools
from weft.domain import (Checkpoint, ContractError, EventEnvelope, NotFound, RunState,
                         StateConflict, SubmissionConflict, TaskSpec, canonical_json,
                         operation_id, validate_transition)
from weft.storage.migrations import MigrationError, migrate
from weft.storage.sqlite import SQLiteExecutionRepository


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = SQLiteExecutionRepository(self.root)
        self.spec = TaskSpec("create report", "workspace", {"model": "fixed", "budget": 10})

    def submit(self, **kwargs):
        return self.repo.submit("thread", self.spec, **kwargs)

    def start(self):
        submission = self.submit()
        return self.repo.start_attempt(submission.run.run_id, 0, "worker-a")

    def end(self, run, attempt, target):
        return self.repo.transition(run.run_id, run.version, target, attempt_id=attempt.attempt_id)

    def test_submission_snapshot_is_detached_and_reopens(self):
        submission = self.submit()
        self.spec.configuration["budget"] = 99
        reopened = SQLiteExecutionRepository(self.root)
        self.assertEqual(reopened.get_task(submission.task.task_id), submission.task)
        self.assertEqual(json.loads(submission.task.request_json)["configuration"]["budget"], 10)
        self.assertEqual(reopened.get_run(submission.run.run_id), submission.run)
        self.assertEqual(self.repo.attempts(submission.run.run_id), [])

    def test_idempotency_covers_configuration_acceptance_and_workspace(self):
        original = self.submit(idempotency_key="key")
        replay = self.repo.submit("thread", TaskSpec("create report", "workspace", {"budget": 10, "model": "fixed"}),
                                  idempotency_key="key")
        self.assertTrue(original.created)
        self.assertFalse(replay.created)
        self.assertEqual(original.run, replay.run)
        for spec in (TaskSpec("different", "workspace"), TaskSpec("create report", "other"),
                     TaskSpec("create report", "workspace", {"budget": 11}),
                     TaskSpec("create report", "workspace", acceptance={"required": True})):
            with self.subTest(spec=spec), self.assertRaises(SubmissionConflict):
                self.repo.submit("thread", spec, idempotency_key="key")

    def test_key_scope_and_intentional_duplicate(self):
        first = self.submit(idempotency_key="key")
        other = self.repo.submit("other-thread", self.spec, idempotency_key="key")
        unkeyed = [self.submit() for _ in range(2)]
        self.assertEqual(len({first.task.task_id, other.task.task_id, *(s.task.task_id for s in unkeyed)}), 4)

    def test_attempt_finishes_atomically_with_run(self):
        run, attempt = self.start()
        self.assertEqual((run.state, run.version, run.attempt_count), (RunState.RUNNING, 1, 1))
        finished = self.end(run, attempt, RunState.SUCCEEDED)
        reopened = SQLiteExecutionRepository(self.root)
        self.assertEqual(reopened.get_run(run.run_id), finished)
        actual = reopened.attempts(run.run_id)[0]
        self.assertEqual(actual.state, "succeeded")
        self.assertIsNotNone(actual.ended_at)

    def test_pause_resume_preserves_run_but_changes_attempt(self):
        run, first = self.start()
        requested = self.repo.transition(run.run_id, run.version, RunState.PAUSE_REQUESTED)
        paused = self.end(requested, first, RunState.PAUSED)
        queued = self.repo.transition(run.run_id, paused.version, RunState.QUEUED)
        resumed, second = self.repo.start_attempt(run.run_id, queued.version, "worker-b")
        self.assertEqual(resumed.task_id, run.task_id)
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(second.number, 2)
        self.assertEqual([a.state for a in self.repo.attempts(run.run_id)], ["paused", "running"])
        with self.assertRaises(StateConflict):
            self.repo.transition(run.run_id, resumed.version, RunState.FAILED, attempt_id=first.attempt_id)

    def test_interrupted_run_can_be_requeued(self):
        run, attempt = self.start()
        stopped = self.end(run, attempt, RunState.INTERRUPTED)
        queued = self.repo.transition(run.run_id, stopped.version, RunState.QUEUED)
        self.assertEqual(queued.run_id, run.run_id)

    def test_cancellation_wins_and_survives_interruption(self):
        run, attempt = self.start()
        requested = self.repo.transition(run.run_id, run.version, RunState.CANCEL_REQUESTED)
        with self.assertRaises(StateConflict):
            self.end(requested, attempt, RunState.SUCCEEDED)
        stopped = self.end(requested, attempt, RunState.INTERRUPTED)
        with self.assertRaises(StateConflict):
            self.repo.transition(run.run_id, stopped.version, RunState.QUEUED)
        cancelled = self.repo.transition(run.run_id, stopped.version, RunState.CANCELLED)
        self.assertTrue(cancelled.cancellation_requested)

    def test_queued_pause_cancel_do_not_create_attempt(self):
        submission = self.submit()
        paused = self.repo.transition(submission.run.run_id, 0, RunState.PAUSED)
        cancelled = self.repo.transition(paused.run_id, paused.version, RunState.CANCELLED)
        self.assertEqual(self.repo.attempts(cancelled.run_id), [])

    def test_terminal_runs_are_immutable_and_rerun_is_explicit(self):
        for target in (RunState.SUCCEEDED, RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED):
            with self.subTest(target=target):
                run, attempt = self.start()
                terminal = self.end(run, attempt, target)
                for next_state in RunState:
                    with self.assertRaises(StateConflict):
                        self.repo.transition(run.run_id, terminal.version, next_state)
                new = self.repo.rerun(run.task_id, run.run_id)
                self.assertNotEqual(new.run_id, run.run_id)
                self.assertEqual(new.previous_run_id, run.run_id)
                with self.assertRaises(StateConflict):
                    self.repo.rerun(run.task_id, run.run_id)

    def test_rerun_rejects_active_or_foreign_task(self):
        run, attempt = self.start()
        with self.assertRaises(StateConflict):
            self.repo.rerun(run.task_id, run.run_id)
        self.end(run, attempt, RunState.FAILED)
        other = self.submit()
        with self.assertRaises(StateConflict):
            self.repo.rerun(other.task.task_id, run.run_id)

    def test_keyed_resubmission_returns_original_run_after_rerun(self):
        submitted = self.submit(idempotency_key="key")
        run, attempt = self.repo.start_attempt(submitted.run.run_id, 0, "worker")
        self.end(run, attempt, RunState.FAILED)
        self.repo.rerun(run.task_id, run.run_id)
        self.assertEqual(self.submit(idempotency_key="key").run.run_id, run.run_id)

    def test_stale_version_and_owner_cannot_mutate(self):
        run, attempt = self.start()
        for version, identity in ((0, attempt.attempt_id), (run.version, "wrong"), (run.version, None)):
            with self.subTest(version=version, identity=identity), self.assertRaises(StateConflict):
                self.repo.transition(run.run_id, version, RunState.FAILED, attempt_id=identity)
        self.assertEqual(self.repo.get_run(run.run_id), run)
        self.assertEqual(self.repo.attempts(run.run_id)[0].state, "running")

    def test_transaction_rolls_back_attempt_if_run_update_fails(self):
        submission = self.submit()
        with closing(sqlite3.connect(self.repo.path)) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_update BEFORE UPDATE ON runs BEGIN
                            SELECT RAISE(ABORT, 'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.start_attempt(submission.run.run_id, 0, "worker")
        self.assertEqual(self.repo.get_run(submission.run.run_id), submission.run)
        self.assertEqual(self.repo.attempts(submission.run.run_id), [])

    def test_concurrent_submission_and_start_have_single_winner(self):
        barrier = threading.Barrier(8)
        def submit(_):
            repo = SQLiteExecutionRepository(self.root)
            barrier.wait()
            return repo.submit("thread", self.spec, idempotency_key="race")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(sum(s.created for s in results), 1)
        self.assertEqual(len({s.run.run_id for s in results}), 1)
        run_id = results[0].run.run_id
        def start(i):
            barrier.wait()
            try:
                SQLiteExecutionRepository(self.root).start_attempt(run_id, 0, f"worker-{i}")
                return True
            except StateConflict:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = list(pool.map(start, range(8)))
        self.assertEqual(sum(winners), 1)
        self.assertEqual(len(self.repo.attempts(run_id)), 1)

    def test_not_found_and_invalid_mutation_contracts(self):
        with self.assertRaises(NotFound):
            self.repo.get_run("missing")
        submission = self.submit()
        with self.assertRaises(ContractError):
            self.repo.transition(submission.run.run_id, 0, RunState.RUNNING)
        for version in (-1, True, 1.2):
            with self.subTest(version=version), self.assertRaises(ContractError):
                self.repo.start_attempt(submission.run.run_id, version, "worker")


class ContractTests(unittest.TestCase):
    def test_transition_matrix_matches_independent_specification(self):
        allowed = {
            "queued": {"running", "paused", "cancelled"},
            "running": {"pause_requested", "cancel_requested", "interrupted", "succeeded", "failed", "cancelled", "timed_out"},
            "pause_requested": {"paused", "cancel_requested", "interrupted", "succeeded", "failed", "cancelled", "timed_out"},
            "cancel_requested": {"cancelled", "interrupted"},
            "paused": {"queued", "cancelled"}, "interrupted": {"queued", "cancelled"},
        }
        for source in RunState:
            for target in RunState:
                with self.subTest(source=source, target=target):
                    if target.value in allowed.get(source.value, set()):
                        validate_transition(source, target)
                    else:
                        with self.assertRaises(StateConflict):
                            validate_transition(source, target)

    def test_operation_identity_survives_attempts_and_separates_repetitions(self):
        self.assertEqual(operation_id("run", "step:1/call:1"), operation_id("run", "step:1/call:1"))
        self.assertNotEqual(operation_id("run", "step:1/call:1"), operation_id("run", "step:2/call:1"))
        self.assertNotEqual(operation_id("run", "step:1/call:1"), operation_id("rerun", "step:1/call:1"))

    def test_invalid_request_is_rejected_before_storage(self):
        for spec in (TaskSpec("", "w"), TaskSpec("x", ""), TaskSpec("x", "w", {1: "bad"}),
                     TaskSpec("x", "w", {"n": float("nan")}), TaskSpec("x", "w", schema_version=2),
                     TaskSpec("x", "w", schema_version=True), TaskSpec("x", "w", acceptance=[])):
            with self.subTest(spec=spec), self.assertRaises(ContractError):
                spec.snapshot()

    def test_versioned_event_and_checkpoint_payloads(self):
        event = EventEnvelope("task", "run", "attempt", 1, "model.completed", '{"usage":2}')
        checkpoint = Checkpoint("task", "run", "attempt", 1, "execute_tools", '{"messages":[]}', "0.1.0")
        self.assertEqual(event.schema_version, checkpoint.schema_version)
        for kwargs in ({"sequence": 0}, {"sequence": True}, {"data_json": "[]"},
                       {"data_json": '{"x":NaN}'}, {"schema_version": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                EventEnvelope(**{**event.__dict__, **kwargs})
        with self.assertRaises(ContractError):
            Checkpoint(**{**checkpoint.__dict__, "event_sequence": -1})


class MigrationTests(unittest.TestCase):
    def test_legacy_database_adoption_preserves_both_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "threads.sqlite3")) as conn, conn:
                conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, messages TEXT NOT NULL)")
                conn.execute("CREATE TABLE contexts(id TEXT PRIMARY KEY, state TEXT NOT NULL)")
                conn.execute("INSERT INTO threads VALUES ('old', ?)", ('[{"role":"user","content":"keep"}]',))
                conn.execute("INSERT INTO contexts VALUES ('old', ?)", ('{"summary":"keep"}',))
            for _ in range(2):
                store = Store(root)
                self.assertEqual(store.load("old")[0]["content"], "keep")
                self.assertEqual(store.load_context("old"), {"summary": "keep"})
            with closing(sqlite3.connect(store.db)) as conn, conn:
                self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,)])

    def test_failed_migration_rolls_back_ddl_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                migrate(path, (("CREATE TABLE partial(id INTEGER)", "INVALID SQL"),))
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
            migrate(path, (("CREATE TABLE valid(id INTEGER)",),))

    def test_newer_or_gapped_schema_is_refused(self):
        for versions in ((1, 2), (2,)):
            with self.subTest(versions=versions), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "db.sqlite3"
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                    conn.executemany("INSERT INTO schema_migrations VALUES (?, 'now')", [(v,) for v in versions])
                with self.assertRaises(MigrationError):
                    migrate(path, (("CREATE TABLE forbidden(id INTEGER)",),))


class EmbeddedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_records_identity_success_failure_and_cancellation(self):
        class Model:
            name = "fixed-offline"
            def complete(self, *args, **kwargs):
                return Reply({"role": "assistant", "content": "done"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SQLiteExecutionRepository(root)
            agent = Agent(Model(), ToolExecutor(FileTools(root / "files").registry()), Store(root), executions=repo)
            for cancel, expected in ((None, RunState.SUCCEEDED), (threading.Event(), RunState.CANCELLED)):
                if cancel is not None:
                    cancel.set()
                result = await agent.arun("hello", cancel=cancel)
                self.assertEqual(repo.get_run(result.run_id).state, expected)
                self.assertEqual(repo.get_task(result.task_id).thread_id, result.thread_id)
                self.assertEqual(repo.attempts(result.run_id)[0].attempt_id, result.attempt_id)
                events = [json.loads(line) for line in Path(result.event_path).read_text(encoding="utf-8").splitlines()]
                self.assertTrue(all(e["task_id"] == result.task_id and e["attempt_id"] == result.attempt_id for e in events))
            with patch.object(Model, "complete", side_effect=RuntimeError("private")):
                result = await agent.arun("fail")
            self.assertEqual(repo.get_run(result.run_id).state, RunState.FAILED)
            self.assertEqual(repo.get_run(result.run_id).reason, "error")

    async def test_cancel_during_database_start_closes_committed_attempt(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingRepository(SQLiteExecutionRepository):
            captured_run = None
            def start_attempt(self, *args):
                result = super().start_attempt(*args)
                self.captured_run = result[0].run_id
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test synchronization timeout")
                return result
        class Model:
            name = "must-not-call"
            def complete(self, *args, **kwargs):
                raise AssertionError("Model must not execute")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = BlockingRepository(root)
            agent = Agent(Model(), ToolExecutor(FileTools(root / "files").registry()), Store(root), executions=repo)
            task = asyncio.create_task(agent.arun("cancel during start"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(repo.get_run(repo.captured_run).state, RunState.CANCELLED)
                self.assertEqual(repo.attempts(repo.captured_run)[0].state, "cancelled")
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


class CLIIntegrationTests(unittest.TestCase):
    def test_real_cli_demo_persists_identity_and_artifact(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = subprocess.run(
                [sys.executable, str(project / "main.py"), "--demo", "--thread", "acceptance",
                 "--workspace", str(root / "files"), "--state-dir", str(root / "state")],
                cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=30,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = (root / "files" / "report.md").read_text(encoding="utf-8")
            self.assertIn((root / "files" / "hello.txt").read_text(encoding="utf-8").strip(), report)
            paths = list((root / "state" / "runs").glob("*.jsonl"))
            self.assertEqual(len(paths), 1)
            events = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
            end = events[-1]
            self.assertEqual((end["event"], end["status"]), ("run.end", "completed"))
            repo = SQLiteExecutionRepository(root / "state")
            run = repo.get_run(end["run_id"])
            self.assertEqual(run.state, RunState.SUCCEEDED)
            self.assertEqual(run.task_id, end["task_id"])
            attempts = repo.attempts(run.run_id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual((attempts[0].attempt_id, attempts[0].state), (end["attempt_id"], "succeeded"))
            self.assertIsNotNone(attempts[0].ended_at)
            snapshot = json.loads(repo.get_task(run.task_id).request_json)
            self.assertEqual(snapshot["workspace"], str((root / "files").resolve()))
            self.assertTrue(Store(root / "state").load("acceptance"))


if __name__ == "__main__":
    unittest.main()

