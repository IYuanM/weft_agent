"""Completed-turn snapshots and append-only execution events have different jobs."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        self.root = root
        (root / "runs").mkdir(parents=True, exist_ok=True)
        self.db = root / "threads.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, messages TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS contexts (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def load(self, thread_id: str) -> list[dict]:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute("SELECT messages FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def load_context(self, thread_id: str) -> dict:
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT state FROM contexts WHERE id = ?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, thread_id: str, messages: list[dict], context_state=None) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO threads VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET messages=excluded.messages",
                         (thread_id, json.dumps(messages, ensure_ascii=False)))
            conn.execute("INSERT INTO contexts VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                         (thread_id, json.dumps(context_state or {}, ensure_ascii=False)))


