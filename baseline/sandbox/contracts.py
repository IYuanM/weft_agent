from dataclasses import dataclass
from typing import Protocol
from ..core.run_context import RunContext


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 10
    output_bytes: int = 16_000
    code_bytes: int = 32_000
    memory_mb: int = 256
    pids: int = 32
    cpus: float = 1.0

    def __post_init__(self):
        if not 0 < self.timeout_seconds <= 300 or not 0 < self.cpus <= 8:
            raise ValueError("Invalid sandbox time/CPU limits")
        if not 256 <= self.output_bytes <= 24_000 or not 1 <= self.code_bytes <= 100_000:
            raise ValueError("Invalid sandbox output/code limits")
        if not 64 <= self.memory_mb <= 4096 or not 1 <= self.pids <= 256:
            raise ValueError("Invalid sandbox resource limits")


@dataclass(frozen=True)
class Execution:
    code: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    cleanup_ok: bool = True


class Sandbox(Protocol):
    limits: SandboxLimits
    async def run_python(self, code: str, context: RunContext) -> Execution: ...
