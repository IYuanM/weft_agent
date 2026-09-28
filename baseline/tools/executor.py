"""Single gate for validation, authorization, bounded retries and events."""
import asyncio
import hashlib
import json
import time
from ..core.run_context import RunContext
from .contracts import ToolFailure, ToolResult
from .registry import ToolRegistry
from .concurrency import Admission, invoke, WorkerInterrupted


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, max_attempts=2, retry_delay=0.1,
                 max_argument_chars=200_000, max_result_chars=64_000, admission=None, max_parallel=3):
        if not 1 <= max_attempts <= 5 or not 0 <= retry_delay <= 5:
            raise ValueError("Invalid retry policy")
        if min(max_argument_chars, max_result_chars) < 256:
            raise ValueError("Tool limits must be at least 256 characters")
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
            raise ValueError("max_parallel must be a positive integer")
        self.admission = admission or Admission()
        self.max_parallel = max_parallel
        self.registry = registry
        self.max_attempts, self.retry_delay = max_attempts, retry_delay
        self.max_argument_chars, self.max_result_chars = max_argument_chars, max_result_chars

    async def aexecute(self, name: str, raw_arguments: str, *, context: RunContext,
                tool_call_id: str, step: int, emit) -> ToolResult:
        meta = {"tool": name, "tool_call_id": tool_call_id, "step": step}
        started = time.monotonic()
        attempts = 0
        result = ToolResult(False, "internal_error")
        emit("tool.start", **meta)
        try:
            reason = context.stop_reason()
            if reason:
                raise ToolFailure(reason)
            if context.tool_calls >= context.max_tool_calls:
                raise ToolFailure("tool_budget_exceeded")
            context.tool_calls += 1
            spec = self.registry.get(name)
            if spec is None:
                raise ToolFailure("unknown_tool")
            if len(raw_arguments) > self.max_argument_chars:
                raise ToolFailure("arguments_too_large")
            try:
                args = json.loads(raw_arguments, object_pairs_hook=self._unique_object)
            except (ValueError, TypeError, RecursionError):
                raise ToolFailure("invalid_arguments") from None
            if (not isinstance(args, dict) or set(args) != {key for key, _ in spec.parameters}
                    or any(not isinstance(value, str) for value in args.values())):
                raise ToolFailure("invalid_arguments")
            deadline = min(context.deadline, started + spec.timeout_seconds)
            resource = spec.authorize(args, context)
            emit("tool.queued", **meta, resource_id=hashlib.sha256(resource.encode()).hexdigest()[:16])
            async with self.admission.acquire(resource, context, deadline):
                for attempt in range(1, self.max_attempts + 1):
                    reason = context.stop_reason()
                    if reason or time.monotonic() >= deadline:
                        result = ToolResult(False, reason or "timeout")
                        break
                    if spec.authorize(args, context) != resource:
                        raise ToolFailure("resource_changed")
                    if not spec.read_only and resource in context.uncertain_resources:
                        raise ToolFailure("previous_effect_unknown", effect="unknown")
                    resource_id = hashlib.sha256(resource.encode()).hexdigest()[:16]
                    write_key = hashlib.sha256(json.dumps([name, resource, args], sort_keys=True).encode()).hexdigest()
                    if not spec.read_only and write_key in context.completed_writes:
                        result = ToolResult(False, "duplicate_write", artifacts=context.completed_writes[write_key],
                                            effect="confirmed")
                        break
                    # Authorization can take time or observe cancellation itself.
                    reason = context.stop_reason()
                    if reason or time.monotonic() >= deadline:
                        result = ToolResult(False, reason or "timeout")
                        break
                    attempts = attempt
                    emit("tool.attempt", **meta, attempt=attempt, resource_id=resource_id)
                    entered = time.monotonic()
                    interrupted = None
                    try:
                        result, interrupted = await invoke(spec.handler, args, context, deadline)
                        if result is None and interrupted:
                            result = ToolResult(False, interrupted, effect="none" if spec.read_only else "unknown")
                        if not isinstance(result, ToolResult):
                            raise TypeError("Handler must return ToolResult")
                        encoded = json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)
                        if len(encoded) > self.max_result_chars:
                            result = ToolResult(False, "result_too_large", effect=result.effect)
                    except ToolFailure as exc:
                        result = ToolResult(False, exc.code, retryable=exc.retryable, effect=exc.effect)
                    except WorkerInterrupted:
                        context.cancel.set()
                        result = ToolResult(False, "cancelled", effect="none" if spec.read_only else "unknown")
                    except Exception as exc:
                        result = self._exception_result(exc, spec.read_only)
                    retry = (not result.ok and result.retryable and spec.read_only and spec.idempotent
                             and result.effect == "none")
                    result = ToolResult(result.ok, result.code, result.content, retry,
                                        result.artifacts, result.effect)
                    if not spec.read_only and result.effect == "unknown":
                        context.uncertain_resources.add(resource)
                    if not spec.read_only and result.effect == "confirmed":
                        context.completed_writes[write_key] = result.artifacts
                    emit("tool.attempt.end", **meta, attempt=attempt, ok=result.ok, code=result.code,
                         effect=result.effect, duration_ms=round((time.monotonic() - entered) * 1000))
                    reason = context.stop_reason() or interrupted
                    if reason or time.monotonic() >= deadline:
                        result = ToolResult(False, reason or "timeout", artifacts=result.artifacts, effect=result.effect)
                        break
                    if not retry or attempt == self.max_attempts:
                        break
                    delay = self.retry_delay * (2 ** (attempt - 1))
                    if time.monotonic() + delay >= deadline:
                        break
                    emit("tool.retry", **meta, attempt=attempt, code=result.code, delay_seconds=delay)
                    until = time.monotonic() + delay
                    while time.monotonic() < until and not context.stop_reason():
                        await asyncio.sleep(min(0.02, until - time.monotonic()))
        except ToolFailure as exc:
            result = ToolResult(False, exc.code, effect=exc.effect)
        except asyncio.CancelledError:
            context.cancel.set()
            result = ToolResult(False, "cancelled", effect="unknown" if attempts else "none")
            if attempts and not spec.read_only:
                context.uncertain_resources.add(resource)
            raise
        except KeyboardInterrupt:
            context.cancel.set()
            result = ToolResult(False, "cancelled", effect="unknown" if attempts else "none")
        except Exception:
            result = ToolResult(False, "internal_error", effect="unknown" if attempts else "none")
        finally:
            emit("tool.end", **meta, ok=result.ok, code=result.code, error=None if result.ok else result.code,
                 attempts=attempts, effect=result.effect, artifacts=list(result.artifacts),
                 result_chars=len(json.dumps(result.to_dict(), ensure_ascii=False)),
                 duration_ms=round((time.monotonic() - started) * 1000))
        return result

    def execute(self, *args, **kwargs):
        """Synchronous CLI/test adapter. Async callers must await aexecute()."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aexecute(*args, **kwargs))
        raise RuntimeError("Use await aexecute() inside an event loop")

    async def execute_batch(self, calls, *, context, step, emit):
        """Read-only opt-in waves; each write/non-parallel tool is a batch barrier."""
        results = []
        wave = []
        async def flush():
            if not wave:
                return
            tasks = [asyncio.create_task(self.aexecute(
                call["function"]["name"], call["function"]["arguments"], context=context,
                tool_call_id=call["id"], step=step, emit=emit)) for call in wave]
            try:
                results.extend(await asyncio.gather(*tasks))
            except BaseException:
                context.cancel.set()
                for task in tasks:
                    task.cancel()
                from .concurrency import drain
                for task in tasks:
                    await drain(task)
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            wave.clear()
        for call in calls:
            spec = self.registry.get(call["function"]["name"])
            parallel = bool(spec and spec.read_only and spec.parallel_safe)
            if not parallel:
                await flush()
            wave.append(call)
            if not parallel or len(wave) >= self.max_parallel:
                await flush()
        await flush()
        return results

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    @staticmethod
    def _exception_result(exc, read_only):
        code = "internal_error"
        if isinstance(exc, FileNotFoundError):
            code = "not_found"
        elif isinstance(exc, FileExistsError):
            code = "already_exists"
        elif isinstance(exc, PermissionError):
            code = "permission_denied"
        elif isinstance(exc, (IsADirectoryError, NotADirectoryError)):
            code = "invalid_path"
        elif isinstance(exc, UnicodeError):
            code = "invalid_encoding"
        elif isinstance(exc, (TimeoutError, ConnectionError)):
            code = "transient_error"
        elif isinstance(exc, OSError):
            code = "io_error"
        return ToolResult(False, code, retryable=read_only and code == "transient_error",
                          effect="none" if read_only else "unknown")
