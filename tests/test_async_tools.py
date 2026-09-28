import asyncio
import time
import unittest
from dataclasses import replace
from threading import Event

from baseline.core.run_context import RunContext
from baseline.models.chat import DemoModel
from baseline.tools.contracts import ToolResult, ToolSpec
from baseline.tools.registry import ToolRegistry
from baseline.tools.executor import ToolExecutor
from baseline.tools.concurrency import Admission


class AsyncToolTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return RunContext("thread", "run", Event(), time.monotonic() + 5)

    def spec(self, name, handler, **kwargs):
        return ToolSpec(name, name, (("path", "path"),), handler, lambda args, _: args["path"], **kwargs)

    def call(self, name, path, ident=None):
        return DemoModel.call(ident or path, name, {"path": path}).message["tool_calls"][0]

    async def batch(self, executor, calls, context=None, emit=lambda *a, **k: None):
        return await asyncio.wait_for(executor.execute_batch(calls, context=context or self.context(), step=1, emit=emit), 3)

    async def test_independent_reads_overlap_and_results_keep_input_order(self):
        entered = set()
        gate = asyncio.Event()
        async def read(args, context):
            entered.add(args["path"])
            if len(entered) == 2:
                gate.set()
            await gate.wait()
            if args["path"] == "first":
                await asyncio.sleep(0.01)
            return ToolResult(True, content={"path": args["path"]})
        executor = ToolExecutor(ToolRegistry([self.spec("read", read, parallel_safe=True)]))
        results = await self.batch(executor, [self.call("read", "first"), self.call("read", "second")])
        self.assertEqual([r.content["path"] for r in results], ["first", "second"])
        self.assertEqual(executor.admission.locks, {})

    async def test_global_capacity_shared_across_executors(self):
        active = peak = 0
        async def read(args, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(True)
        admission = Admission(2)
        registry = ToolRegistry([self.spec("read", read, parallel_safe=True)])
        a, b = [ToolExecutor(registry, admission=admission, max_parallel=4) for _ in range(2)]
        await asyncio.gather(self.batch(a, [self.call("read", str(i)) for i in range(4)]),
                             self.batch(b, [self.call("read", str(i)) for i in range(4, 8)]))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    async def test_same_resource_serializes_across_runs(self):
        active = peak = 0
        async def read(args, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(True)
        executor = ToolExecutor(ToolRegistry([self.spec("read", read, parallel_safe=True)]))
        await asyncio.gather(self.batch(executor, [self.call("read", "same")]),
                             self.batch(executor, [self.call("read", "same")]))
        self.assertEqual(peak, 1)

    async def test_write_is_barrier_even_across_distinct_resources(self):
        trace = []
        async def handler(args, context):
            trace.append("start-" + args["path"])
            await asyncio.sleep(0)
            trace.append("end-" + args["path"])
            return ToolResult(True)
        registry = ToolRegistry([self.spec("read", handler, parallel_safe=True),
                                 self.spec("write", handler, read_only=False, idempotent=False)])
        executor = ToolExecutor(registry)
        await self.batch(executor, [self.call("read", "a"), self.call("read", "b"),
                                   self.call("write", "c"), self.call("read", "d")])
        self.assertLess(trace.index("end-a"), trace.index("start-c"))
        self.assertLess(trace.index("end-b"), trace.index("start-c"))
        self.assertLess(trace.index("end-c"), trace.index("start-d"))

    async def test_failure_does_not_discard_sibling_results(self):
        async def read(args, context):
            if args["path"] == "missing":
                raise FileNotFoundError()
            return ToolResult(True)
        executor = ToolExecutor(ToolRegistry([self.spec("read", read, parallel_safe=True)]))
        results = await self.batch(executor, [self.call("read", "missing"), self.call("read", "good")])
        self.assertEqual(results[0].code, "not_found")
        self.assertTrue(results[1].ok)

    async def test_cancel_event_stops_native_and_prevents_queued_work(self):
        started, cleaned = asyncio.Event(), asyncio.Event()
        count = 0
        async def read(args, context):
            nonlocal count
            count += 1
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.set()
        executor = ToolExecutor(ToolRegistry([self.spec("read", read, parallel_safe=True)]), max_parallel=1)
        context = self.context()
        task = asyncio.create_task(self.batch(executor, [self.call("read", "a"), self.call("read", "b")], context))
        await started.wait()
        context.cancel.set()
        results = await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(count, 1)
        self.assertTrue(all(r.code == "cancelled" for r in results))

    async def test_native_timeout_cleans_up(self):
        cleaned = asyncio.Event()
        async def read(args, context):
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.set()
        executor = ToolExecutor(ToolRegistry([self.spec("read", read, timeout_seconds=0.03)]))
        results = await self.batch(executor, [self.call("read", "a")])
        self.assertEqual(results[0].code, "timeout")
        self.assertTrue(cleaned.is_set())
        self.assertEqual(executor.admission.locks, {})

    async def test_cancelled_sync_write_keeps_lock_until_thread_exits(self):
        started, release = Event(), Event()
        reader_entered = asyncio.Event()
        def write(args, context):
            started.set()
            release.wait(2)
            return ToolResult(True, effect="confirmed", artifacts=("file",))
        async def read(args, context):
            reader_entered.set()
            return ToolResult(True)
        executor = ToolExecutor(ToolRegistry([self.spec("write", write, read_only=False), self.spec("read", read)]))
        context = self.context()
        first = asyncio.create_task(self.batch(executor, [self.call("write", "same")], context))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            context.cancel.set()
            second = asyncio.create_task(self.batch(executor, [self.call("read", "same")]))
            await asyncio.sleep(0.04)
            self.assertFalse(first.done())
            self.assertFalse(reader_entered.is_set())
        finally:
            release.set()
        result = (await first)[0]
        await second
        self.assertEqual(result.code, "cancelled")
        self.assertEqual(result.effect, "confirmed")
        self.assertTrue(reader_entered.is_set())

    async def test_external_task_cancellation_drains_all_children(self):
        entered = asyncio.Event()
        active = 0
        async def read(args, context):
            nonlocal active
            active += 1
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                active -= 1
        executor = ToolExecutor(ToolRegistry([self.spec("read", read, parallel_safe=True)]))
        task = asyncio.create_task(executor.execute_batch([self.call("read", "a"), self.call("read", "b")],
                                   context=self.context(), step=1, emit=lambda *a, **k: None))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(active, 0)
        self.assertEqual(executor.admission.active, 0)
        self.assertEqual(executor.admission.locks, {})

    async def test_expired_queued_tool_never_enters_handler(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(args, context):
            calls.append(context.run_id)
            entered.set()
            await release.wait()
            return ToolResult(True)
        executor = ToolExecutor(ToolRegistry([self.spec("read", handler)]))
        first = asyncio.create_task(self.batch(executor, [self.call("read", "same")]))
        await entered.wait()
        context = self.context()
        context.run_id = "queued"
        context.deadline = time.monotonic() + 0.03
        try:
            result = (await self.batch(executor, [self.call("read", "same")], context))[0]
            self.assertEqual(result.code, "timeout")
            self.assertNotIn("queued", calls)
        finally:
            release.set()
            await first
