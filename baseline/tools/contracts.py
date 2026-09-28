"""Explicit contracts shared by tools and their executor."""
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal
from ..core.run_context import RunContext


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    code: str = "ok"
    content: dict = field(default_factory=dict)
    retryable: bool = False
    artifacts: tuple[str, ...] = ()
    effect: Literal["none", "confirmed", "unknown"] = "none"

    def __post_init__(self):
        if (not isinstance(self.ok, bool) or not isinstance(self.retryable, bool)
                or not isinstance(self.code, str) or not self.code
                or not isinstance(self.content, dict)
                or self.effect not in {"none", "confirmed", "unknown"}
                or not isinstance(self.artifacts, tuple)
                or any(not isinstance(item, str) for item in self.artifacts)):
            raise ValueError("Invalid tool result contract")

    def to_dict(self):
        return {"ok": self.ok, "code": self.code, "content": self.content,
                "retryable": self.retryable, "artifacts": list(self.artifacts), "effect": self.effect}


class ToolFailure(Exception):
    """Controlled metadata only; never pass raw exception text to the model."""
    def __init__(self, code: str, *, retryable=False, effect="none"):
        self.code, self.retryable, self.effect = code, retryable, effect
        super().__init__(code)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # Supported schema subset: required string fields, no additional fields.
    parameters: tuple[tuple[str, str], ...]
    handler: Callable[[dict, RunContext], ToolResult | Awaitable[ToolResult]]
    authorize: Callable[[dict, RunContext], str]
    read_only: bool = True
    idempotent: bool = True
    timeout_seconds: float = 10.0  # cooperative check, not process termination
    allow_repeated_reads: bool = False  # explicit bounded polling; run/step limits still apply
    parallel_safe: bool = False  # only independent read-only operations may opt in

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": {
                name: {"type": "string", "description": description}
                for name, description in self.parameters
            }, "required": [name for name, _ in self.parameters], "additionalProperties": False},
        }}
