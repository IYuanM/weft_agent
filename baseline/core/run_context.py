import time
from dataclasses import dataclass, field
from threading import Event


@dataclass
class RunContext:
    thread_id: str
    run_id: str
    cancel: Event
    deadline: float
    max_tool_calls: int = 64
    tool_calls: int = 0
    uncertain_resources: set[str] = field(default_factory=set)
    completed_writes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    task_id: str = ""
    attempt_id: str = ""

    def stop_reason(self):
        if self.cancel.is_set():
            return "cancelled"
        if time.monotonic() >= self.deadline:
            return "timeout"
        return None


