"""One-event-loop admission and resource locks, plus honest cancellation of sync work."""
import asyncio
import inspect
import time
from contextlib import asynccontextmanager


class WorkerInterrupted(Exception):
    pass


def call_sync(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyboardInterrupt:
        raise WorkerInterrupted() from None


async def drain(task):
    """Never release resources while a thread/coroutine still owns them."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break


async def run_sync(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(call_sync, function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await drain(task)
        # Consume failures from the drained operation.
        if not task.cancelled():
            task.exception()
        raise


async def invoke(handler, args, context, deadline):
    """Returns (result, stop_reason); sync calls finish before their locks are released."""
    native = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(getattr(handler, "__call__", None))
    task = asyncio.create_task(handler(args, context) if native else asyncio.to_thread(call_sync, handler, args, context))
    reason = None
    try:
        while not task.done():
            reason = context.stop_reason() or ("timeout" if time.monotonic() >= deadline else None)
            if reason:
                if native:
                    task.cancel()
                await drain(task)
                break
            await asyncio.wait({task}, timeout=0.02)
        if task.cancelled():
            return None, reason or "cancelled"
        return task.result(), reason
    except asyncio.CancelledError:
        context.cancel.set()
        if native:
            task.cancel()
        await drain(task)
        if not task.cancelled():
            task.exception()
        raise


class Admission:
    """Share this instance across executors to share a capacity/lock boundary."""
    def __init__(self, capacity=4):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self.loop = None
        self.active = 0
        self.locks = {}

    def bind(self):
        loop = asyncio.get_running_loop()
        if loop is not self.loop:
            if self.active:
                raise RuntimeError("Admission cannot be used by concurrent event loops")
            self.loop = loop
            self.slots = asyncio.Semaphore(self.capacity)
            self.locks = {}

    @asynccontextmanager
    async def acquire(self, resource, context, deadline):
        self.bind()
        self.active += 1
        entry = self.locks.setdefault(resource, [asyncio.Lock(), 0])
        entry[1] += 1
        locked = admitted = False
        try:
            # Wait on the resource before taking a global slot: a hot key cannot consume all slots.
            for primitive in (entry[0], self.slots):
                while True:
                    reason = context.stop_reason() or ("timeout" if time.monotonic() >= deadline else None)
                    if reason:
                        from .contracts import ToolFailure
                        raise ToolFailure(reason)
                    try:
                        await asyncio.wait_for(primitive.acquire(), timeout=0.02)
                        break
                    except TimeoutError:
                        continue
                if primitive is entry[0]:
                    locked = True
                else:
                    admitted = True
            yield
        finally:
            if admitted:
                self.slots.release()
            if locked:
                entry[0].release()
            entry[1] -= 1
            if not entry[1]:
                self.locks.pop(resource, None)
            self.active -= 1
