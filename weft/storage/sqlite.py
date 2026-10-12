"""SQLite execution repository. No leases, replay, or side-effect ledger yet."""

import uuid

from pathlib import Path

from weft.domain import (ACTIVE_STATES, TERMINAL_STATES, AttemptRecord, ContractError,
                         NotFound, RunRecord, RunState, StateConflict, Submission,
                         SubmissionConflict, TaskRecord, TaskSpec, integer, nonempty,
                         validate_transition)
from .migrations import EXECUTION_MIGRATIONS, migrate, transaction, utc_now


def _run(row) -> RunRecord:
    return RunRecord(row["run_id"], row["task_id"], RunState(row["state"]), row["version"],
                     row["attempt_count"], row["current_attempt_id"], bool(row["cancellation_requested"]),
                     row["reason"], row["created_at"], row["updated_at"], row["previous_run_id"])


def _task(row) -> TaskRecord:
    return TaskRecord(row["task_id"], row["thread_id"], row["request_json"], row["created_at"])


def _find(conn, table, column, value):
    # Table/column names are internal constants, never caller input.
    row = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (value,)).fetchone()
    if row is None:
        raise NotFound(f"{table} record not found")
    return row


class SQLiteExecutionRepository:
    def __init__(self, root):
        self.path = Path(root) / "executions.sqlite3"
        migrate(self.path, EXECUTION_MIGRATIONS)

    def submit(self, thread_id: str, spec: TaskSpec, *, idempotency_key=None) -> Submission:
        nonempty(thread_id, "thread_id")
        if idempotency_key is not None:
            nonempty(idempotency_key, "idempotency_key")
        snapshot = spec.snapshot()
        with transaction(self.path) as conn:
            if idempotency_key is not None:
                old = conn.execute("SELECT * FROM tasks WHERE thread_id = ? AND idempotency_key = ?",
                                   (thread_id, idempotency_key)).fetchone()
                if old is not None:
                    if old["request_json"] != snapshot:
                        raise SubmissionConflict("Idempotency key request mismatch")
                    # Submission always identifies the original run, including after retries.
                    run = conn.execute("SELECT * FROM runs WHERE task_id = ? AND previous_run_id IS NULL",
                                       (old["task_id"],)).fetchone()
                    return Submission(_task(old), _run(run), False)
            task_id, run_id, now = uuid.uuid4().hex, uuid.uuid4().hex, utc_now()
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                         (task_id, thread_id, snapshot, idempotency_key, now))
            conn.execute("INSERT INTO runs VALUES (?, ?, 'queued', 0, 0, NULL, 0, NULL, ?, ?, NULL)",
                         (run_id, task_id, now, now))
            return Submission(_task(_find(conn, "tasks", "task_id", task_id)),
                              _run(_find(conn, "runs", "run_id", run_id)), True)

    def get_task(self, task_id):
        with transaction(self.path) as conn:
            return _task(_find(conn, "tasks", "task_id", task_id))

    def get_run(self, run_id):
        with transaction(self.path) as conn:
            return _run(_find(conn, "runs", "run_id", run_id))

    def rerun(self, task_id, previous_run_id):
        """Explicit new execution; never reuse a terminal run or its operation IDs."""
        with transaction(self.path) as conn:
            previous = _run(_find(conn, "runs", "run_id", previous_run_id))
            if previous.task_id != task_id or previous.state not in TERMINAL_STATES:
                raise StateConflict("A terminal run of this task is required")
            if conn.execute("SELECT 1 FROM runs WHERE previous_run_id=?", (previous_run_id,)).fetchone():
                raise StateConflict("This run already has a successor")
            run_id, now = uuid.uuid4().hex, utc_now()
            conn.execute("INSERT INTO runs VALUES (?, ?, 'queued', 0, 0, NULL, 0, NULL, ?, ?, ?)",
                         (run_id, task_id, now, now, previous_run_id))
            return _run(_find(conn, "runs", "run_id", run_id))

    def attempts(self, run_id):
        with transaction(self.path) as conn:
            _find(conn, "runs", "run_id", run_id)
            return [AttemptRecord(**dict(row)) for row in conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY number", (run_id,))]

    def start_attempt(self, run_id, expected_version, owner_id):
        nonempty(owner_id, "owner_id")
        integer(expected_version, "expected_version")
        with transaction(self.path) as conn:
            run = self._checked(conn, run_id, expected_version)
            validate_transition(run.state, RunState.RUNNING)
            if run.cancellation_requested:
                raise StateConflict("Cancellation is pending")
            attempt_id, now = uuid.uuid4().hex, utc_now()
            number = run.attempt_count + 1
            conn.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, 'running', ?, NULL)",
                         (attempt_id, run_id, number, owner_id, now))
            conn.execute("""UPDATE runs SET state='running', version=version+1, attempt_count=?,
                         current_attempt_id=?, reason=NULL, updated_at=? WHERE run_id=?""",
                         (number, attempt_id, now, run_id))
            return (_run(_find(conn, "runs", "run_id", run_id)),
                    AttemptRecord(attempt_id, run_id, number, owner_id, "running", now, None))

    def transition(self, run_id, expected_version, target, *, attempt_id=None, reason=None):
        integer(expected_version, "expected_version")
        if reason is not None:
            nonempty(reason, "reason")
        with transaction(self.path) as conn:
            run = self._checked(conn, run_id, expected_version)
            validate_transition(run.state, target)
            if target == RunState.RUNNING:
                raise ContractError("Use start_attempt to create execution ownership")
            finishing = run.state in ACTIVE_STATES and target not in ACTIVE_STATES
            if finishing:
                if attempt_id != run.current_attempt_id:
                    raise StateConflict("Current attempt required")
                conn.execute("UPDATE attempts SET state=?, ended_at=? WHERE attempt_id=? AND state='running'",
                             (target.value, utc_now(), attempt_id))
            elif attempt_id is not None:
                raise ContractError("attempt_id is only accepted when ending an attempt")
            if target == RunState.QUEUED and run.cancellation_requested:
                raise StateConflict("Interrupted cancellation must be settled as cancelled")
            cancelled = run.cancellation_requested or target in {RunState.CANCEL_REQUESTED, RunState.CANCELLED}
            conn.execute("""UPDATE runs SET state=?, version=version+1, cancellation_requested=?,
                         reason=?, updated_at=? WHERE run_id=?""",
                         (target.value, int(cancelled), reason, utc_now(), run_id))
            return _run(_find(conn, "runs", "run_id", run_id))

    @staticmethod
    def _checked(conn, run_id, expected_version):
        run = _run(_find(conn, "runs", "run_id", run_id))
        if run.version != expected_version:
            raise StateConflict("Stale run version")
        return run
