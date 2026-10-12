"""Versioned execution contracts. No database, asyncio or model dependencies."""

import json
import math
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class ContractError(ValueError):
    pass


class StateConflict(ContractError):
    """Stale version, owner, or an illegal state transition."""


class SubmissionConflict(ContractError):
    """An existing idempotency key was used with a different request."""


class NotFound(ContractError):
    pass


def nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a nonempty string")


def integer(value: int, label: str, minimum=0) -> None:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{label} must be an integer >= {minimum}")


def canonical_json(value: dict) -> str:
    """Reject lossy/non-JSON input instead of silently coercing keys or floats."""
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ContractError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is None or type(item) in (str, bool, int):
            return
        elif type(item) is float and math.isfinite(item):
            return
        else:
            raise ContractError("Expected finite JSON data")
    if not isinstance(value, dict):
        raise ContractError("Expected a JSON object")
    try:
        check(value)
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ContractError("Invalid JSON object") from exc


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATES = frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT})
ACTIVE_STATES = frozenset({RunState.RUNNING, RunState.PAUSE_REQUESTED, RunState.CANCEL_REQUESTED})
TRANSITIONS = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.PAUSED, RunState.CANCELLED}),
    RunState.RUNNING: frozenset({RunState.PAUSE_REQUESTED, RunState.CANCEL_REQUESTED,
                               RunState.INTERRUPTED, *TERMINAL_STATES}),
    RunState.PAUSE_REQUESTED: frozenset({RunState.PAUSED, RunState.CANCEL_REQUESTED,
                                       RunState.INTERRUPTED, *TERMINAL_STATES}),
    # Cancellation wins over completion once the request is committed.
    RunState.CANCEL_REQUESTED: frozenset({RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.PAUSED: frozenset({RunState.QUEUED, RunState.CANCELLED}),
    # An interrupted cancellation must remain cancellation, not resume execution.
    RunState.INTERRUPTED: frozenset({RunState.QUEUED, RunState.CANCELLED}),
    **{state: frozenset() for state in TERMINAL_STATES},
}


def validate_transition(source: RunState, target: RunState) -> None:
    if not isinstance(source, RunState) or not isinstance(target, RunState):
        raise ContractError("Expected RunState")
    if target not in TRANSITIONS[source]:
        raise StateConflict(f"Illegal transition: {source} -> {target}")


@dataclass(frozen=True)
class TaskSpec:
    input: str
    workspace: str
    configuration: dict = field(default_factory=dict)
    acceptance: dict = field(default_factory=dict)
    schema_version: int = 1

    def snapshot(self) -> str:
        nonempty(self.input, "input")
        nonempty(self.workspace, "workspace")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("Unsupported task schema version")
        canonical_json(self.configuration)
        canonical_json(self.acceptance)
        return canonical_json({"schema_version": self.schema_version, "input": self.input,
                               "workspace": self.workspace, "configuration": self.configuration,
                               "acceptance": self.acceptance})


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    thread_id: str
    request_json: str
    created_at: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_id: str
    state: RunState
    version: int
    attempt_count: int
    current_attempt_id: str | None
    cancellation_requested: bool
    reason: str | None
    created_at: str
    updated_at: str
    previous_run_id: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    run_id: str
    number: int
    owner_id: str
    state: str
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class Submission:
    task: TaskRecord
    run: RunRecord
    created: bool


def operation_id(run_id: str, logical_key: str) -> str:
    """Stable across attempts; logical_key distinguishes intentional repetitions."""
    nonempty(run_id, "run_id")
    nonempty(logical_key, "logical_key")
    return uuid.uuid5(uuid.NAMESPACE_URL, canonical_json({"run_id": run_id, "key": logical_key})).hex


@dataclass(frozen=True)
class EventEnvelope:
    task_id: str
    run_id: str
    attempt_id: str
    sequence: int
    kind: str
    data_json: str
    schema_version: int = 1

    def __post_init__(self):
        for name in ("task_id", "run_id", "attempt_id", "kind"):
            nonempty(getattr(self, name), name)
        integer(self.sequence, "sequence", 1)
        _validate_payload(self.schema_version, self.data_json)


@dataclass(frozen=True)
class Checkpoint:
    """Contract only in module 1. Persistence/execution resume arrives in module 2.

    state_json will contain messages, context, loop state, budget consumption,
    pending logical operations and config/tool versions. No monotonic timestamps.
    """
    task_id: str
    run_id: str
    attempt_id: str
    event_sequence: int
    next_action: str
    state_json: str
    runtime_version: str
    schema_version: int = 1

    def __post_init__(self):
        for name in ("task_id", "run_id", "attempt_id", "next_action", "runtime_version"):
            nonempty(getattr(self, name), name)
        integer(self.event_sequence, "event_sequence")
        _validate_payload(self.schema_version, self.state_json)


def _validate_payload(version, payload):
    if type(version) is not int or version != 1:
        raise ContractError("Unsupported contract version")
    try:
        canonical_json(json.loads(payload))
    except (TypeError, ValueError) as exc:
        raise ContractError("Invalid contract payload") from exc
