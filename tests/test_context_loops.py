import copy
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event

from baseline.context.manager import ContextManager, ContextPolicy, ContextLimit, block_ends, estimate_tokens
from baseline.core.agent import Agent
from baseline.models.chat import DemoModel, Reply
from baseline.persistence.store import Store
from baseline.safety.loops import LoopDetector
from baseline.tools.contracts import ToolResult
from baseline.tools.executor import ToolExecutor
from baseline.tools.files import FileTools
from test_baseline import ScriptedModel, answer


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.policy = ContextPolicy(window_tokens=6500, keep_blocks=2, summary_max_chars=500)
        self.events = []
        self.requests = []

    def emit(self, name, **data):
        self.events.append({"event": name, **data})

    def summarize(self, request):
        self.requests.append(request)
        return Reply({"role": "assistant", "content": "约束：使用中文；保留未完成任务。"}, usage={"total_tokens": 7})

    def messages(self):
        return [{"role": "system", "content": "system rules"},
                *[{"role": "user" if i % 2 == 0 else "assistant", "content": str(i) + "x" * 550}
                  for i in range(8)], {"role": "user", "content": "latest exact request"}]

    def test_compaction_preserves_archive_and_latest_request(self):
        raw = self.messages()
        original = copy.deepcopy(raw)
        manager = ContextManager(self.policy)
        projected = manager.prepare(raw, [], summarize=self.summarize, emit=self.emit)
        self.assertEqual(raw, original)
        self.assertEqual(projected[0], raw[0])
        self.assertIn(raw[-1], projected)
        self.assertEqual(projected[1]["role"], "user")
        self.assertLess(estimate_tokens(projected), estimate_tokens(raw))
        self.assertGreater(manager.state["covered"], 0)
        self.assertTrue(self.requests)
        self.assertLessEqual(estimate_tokens(self.requests[0]), self.policy.input_budget)

    def test_tool_block_pairing_artifacts_and_in_turn_user_pin(self):
        raw = [{"role": "system", "content": "rules"}, {"role": "user", "content": "exact constraint"}]
        for i in range(7):
            raw.extend([DemoModel.call(str(i), "read_file", {"path": "a"}).message,
                        {"role": "tool", "tool_call_id": str(i), "content": json.dumps({
                            "ok": True, "content": {"text": "x" * 500}, "artifacts": ["report.md"]})}])
        manager = ContextManager(self.policy)
        projected = manager.prepare(raw, [], summarize=self.summarize, emit=self.emit)
        block_ends(projected[1:])
        self.assertIn(raw[1], projected)
        self.assertIn("report.md", manager.state["artifacts"])
        self.assertIn(manager.state["covered"], block_ends(raw[1:]))

    def test_incomplete_tool_pair_rejected(self):
        raw = [{"role": "system", "content": "s"}, DemoModel.call("x", "read_file", {"path": "x"}).message]
        with self.assertRaises(ContextLimit):
            ContextManager(self.policy).prepare(raw, [], summarize=self.summarize, emit=self.emit)

    def test_summary_failure_falls_back_when_safe(self):
        raw = self.messages()[:7] + [{"role": "user", "content": "latest"}]
        def fail(_):
            raise RuntimeError("private data")
        manager = ContextManager(self.policy)
        projected = manager.prepare(raw, [], summarize=fail, emit=self.emit)
        self.assertEqual(projected, raw)
        self.assertEqual(manager.state, {})
        self.assertEqual(manager.attempts, 1)
        self.assertNotIn("private data", json.dumps(self.events))

    def test_summary_failure_stops_when_oversized(self):
        def fail(_):
            raise RuntimeError()
        with self.assertRaises(ContextLimit):
            ContextManager(self.policy).prepare(self.messages(), [], summarize=fail, emit=self.emit)

    def test_tools_count_toward_budget_and_unsummarizable_request(self):
        with self.assertRaises(ContextLimit):
            ContextManager(self.policy).prepare(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}],
                [{"description": "x" * 6000}], summarize=self.summarize, emit=self.emit)
        self.assertFalse(self.requests)

    def test_truncated_summary_not_committed(self):
        manager = ContextManager(self.policy)
        with self.assertRaises(ContextLimit):
            manager.prepare(self.messages(), [], summarize=lambda _: Reply(
                {"role": "assistant", "content": "partial"}, "length"), emit=self.emit)
        self.assertEqual(manager.state, {})

    def test_snapshot_restore_and_mismatch_invalidation(self):
        manager = ContextManager(self.policy)
        raw = self.messages()
        manager.prepare(raw, [], summarize=self.summarize, emit=self.emit)
        restored = ContextManager(self.policy, copy.deepcopy(manager.state))
        projected = restored.prepare(raw, [], summarize=self.summarize, emit=self.emit)
        self.assertIn("历史摘要", projected[1]["content"])
        raw[1]["content"] = "modified history"
        restored.prepare(raw, [], summarize=self.summarize, emit=self.emit)
        self.assertTrue(any(e["event"] == "context.reset" for e in self.events))


class LoopTests(unittest.TestCase):
    def test_repeated_call_warn_then_stop(self):
        detector = LoopDetector()
        for _ in range(3):
            detector.observe("read", '{"a":1,"b":2}', ToolResult(True, content={"text": "same"}))
        self.assertEqual(detector.check(), ("warn", 1))
        detector.observe("read", '{ "b":2, "a":1 }', ToolResult(True, content={"text": "same"}))
        self.assertEqual(detector.check(), ("stop", 1))

    def test_alternating_loop(self):
        detector = LoopDetector()
        for name in ["a", "b"] * 3:
            detector.observe(name, '{}', ToolResult(True))
        self.assertEqual(detector.check(), ("warn", 2))

    def test_repeated_failure_even_when_arguments_change(self):
        detector = LoopDetector()
        for i in range(3):
            detector.observe("read", json.dumps({"path": str(i)}), ToolResult(False, "not_found"))
        self.assertEqual(detector.check()[0], "warn")

    def test_changed_results_are_progress(self):
        detector = LoopDetector()
        for i in range(10):
            detector.observe("poll", '{}', ToolResult(True, content={"progress": i}))
            self.assertEqual(detector.check(), (None, None))

    def test_explicit_polling_opt_out(self):
        detector = LoopDetector()
        for _ in range(10):
            detector.observe("poll", '{}', ToolResult(True), allow_repeated_reads=True)
            self.assertEqual(detector.check(), (None, None))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store(root / "state")
        self.tools = FileTools(root / "workspace")
        self.executor = ToolExecutor(self.tools.registry())

    def test_loop_stops_without_further_model_call(self):
        model = ScriptedModel(*[DemoModel.call(str(i), "list_files", {"path": "."}) for i in range(4)])
        result = Agent(model, self.executor, self.store).run("list")
        self.assertEqual(result.status, "loop_detected")
        self.assertEqual(len(model.requests), 4)
        self.assertIn("重复", model.requests[3][-1]["content"])
        self.assertEqual(self.store.load("default"), [])

    def test_model_can_recover_after_warning(self):
        model = ScriptedModel(*[DemoModel.call(str(i), "list_files", {"path": "."}) for i in range(3)], answer())
        self.assertEqual(Agent(model, self.executor, self.store).run("list").status, "completed")

    def test_full_history_summary_persistence_and_usage(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 650} for i in range(8)]
        self.store.save("default", history)
        model = ScriptedModel(answer())
        summarizer = ScriptedModel(Reply({"role": "assistant", "content": "prior context"}, usage={"total_tokens": 17}),
                                   Reply({"role": "assistant", "content": "combined context"}, usage={"total_tokens": 19}))
        policy = ContextPolicy(window_tokens=8000, keep_blocks=2)
        result = Agent(model, self.executor, self.store, context_policy=policy, summary_model=summarizer).run("latest")
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.store.load("default")[:8], history)
        self.assertGreater(self.store.load_context("default")["covered"], 0)
        events = [json.loads(line) for line in Path(result.event_path).read_text(encoding="utf-8").splitlines()]
        self.assertGreaterEqual(events[-1]["usage"]["total_tokens"], 17)
        self.assertTrue(any(e["event"] == "context.compaction.end" and e["ok"] for e in events))
        next_model = ScriptedModel(answer("next"))
        resumed = Agent(next_model, self.executor, Store(self.store.root), context_policy=policy).run("followup")
        self.assertEqual(resumed.status, "completed")
        self.assertIn("历史摘要", next_model.requests[0][1]["content"])

    def test_cancellation_after_summary_stops_before_task_model(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 650} for i in range(8)]
        self.store.save("default", history)
        cancel = Event()
        class CancellingSummary:
            def complete(self, messages, tools, timeout):
                cancel.set()
                return Reply({"role": "assistant", "content": "summary"}, usage={"total_tokens": 11})
        model = ScriptedModel()
        result = Agent(model, self.executor, self.store, context_policy=ContextPolicy(window_tokens=8000, keep_blocks=2),
                       summary_model=CancellingSummary()).run("latest", cancel=cancel)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(model.requests, [])
        self.assertEqual(self.store.load("default"), history)
        self.assertEqual(self.store.load_context("default"), {})
        events = [json.loads(line) for line in Path(result.event_path).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["usage"]["total_tokens"], 11)
