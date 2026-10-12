"""Transactional, forward-only SQLite migrations, shared by both databases."""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


class MigrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def transaction(path: Path):
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def migrate(path: Path, migrations: tuple[tuple[str, ...], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with transaction(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        if applied != list(range(1, len(applied) + 1)) or len(applied) > len(migrations):
            raise MigrationError("Unsupported or noncontiguous database schema")
        for version, statements in enumerate(migrations, 1):
            if version <= len(applied):
                continue
            # executescript commits implicitly; execute statements individually.
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, utc_now()))


CONVERSATION_MIGRATIONS = ((
    "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, messages TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS contexts (id TEXT PRIMARY KEY, state TEXT NOT NULL)",
),)

EXECUTION_MIGRATIONS = ((
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
        request_json TEXT NOT NULL, idempotency_key TEXT,
        created_at TEXT NOT NULL, UNIQUE(thread_id, idempotency_key))""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        state TEXT NOT NULL CHECK(state IN ('queued','running','pause_requested','paused',
          'cancel_requested','interrupted','succeeded','failed','cancelled','timed_out')),
        version INTEGER NOT NULL CHECK(version >= 0),
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
        current_attempt_id TEXT REFERENCES attempts(attempt_id),
        cancellation_requested INTEGER NOT NULL CHECK(cancellation_requested IN (0,1)),
        reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        previous_run_id TEXT UNIQUE REFERENCES runs(run_id))""",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
        number INTEGER NOT NULL CHECK(number > 0), owner_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','paused','interrupted','succeeded','failed','cancelled','timed_out')),
        started_at TEXT NOT NULL, ended_at TEXT,
        UNIQUE(run_id, number),
        CHECK((state = 'running' AND ended_at IS NULL) OR (state != 'running' AND ended_at IS NOT NULL)))""",
    "CREATE UNIQUE INDEX one_active_attempt ON attempts(run_id) WHERE state = 'running'",
    "CREATE UNIQUE INDEX one_open_run ON runs(task_id) WHERE state NOT IN ('succeeded','failed','cancelled','timed_out')",
    "CREATE INDEX runs_by_state ON runs(state, created_at)",
),)
