import json
from datetime import datetime, timezone
from weft.storage.contracts import ConversationRepository


class Events:
    def __init__(self, store: ConversationRepository, thread_id: str, run_id: str, observer=None, *,
                 task_id="", attempt_id=""):
        self.path = store.root / "runs" / f"{run_id}.jsonl"
        self.thread_id, self.run_id, self.observer = thread_id, run_id, observer
        self.sequence = 0
        self.task_id, self.attempt_id = task_id, attempt_id

    def emit(self, event: str, **data) -> None:
        self.sequence += 1
        item = {"seq": self.sequence, "time": datetime.now(timezone.utc).isoformat(),
                "schema_version": 1, "thread_id": self.thread_id, "task_id": self.task_id,
                "run_id": self.run_id, "attempt_id": self.attempt_id, "event": event, **data}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
        if self.observer:
            try:
                self.observer(item)
            except Exception:
                pass  # A display callback must not change execution semantics.
