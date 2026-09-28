import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from baseline.models.chat import DemoModel, Reply
from baseline.core.agent import Agent
from baseline.persistence.store import Store
from baseline.tools.files import FileTools
from baseline.tools.executor import ToolExecutor
from baseline.tools.contracts import RunContext
import time


def answer(text="done"):
    return Reply({"role": "assistant", "content": text})


class ScriptedModel:
    name = "test-model"

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools, timeout):
        self.requests.append(json.loads(json.dumps(messages)))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.tools = FileTools(root / "workspace")
        self.store = Store(root / "state")
        self.executor = ToolExecutor(self.tools.registry())

    def execute(self, name, args):
        return self.executor.execute(name, args, context=RunContext("test", "test", Event(), time.monotonic() + 10),
                                     tool_call_id="call", step=1, emit=lambda *a, **kw: None).to_dict()

    def run_agent(self, model, **kwargs):
        return Agent(model, self.executor, self.store, **kwargs).run("hello")

    def events(self, result):
        events = [json.loads(line) for line in Path(result.event_path).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(e["event"] == "run.end" for e in events), 1)
        self.assertEqual(events[-1]["status"], result.status)
        self.assertTrue(all(e["run_id"] == result.run_id for e in events))
        return events

    def test_chat_and_restore_with_new_store(self):
        self.assertEqual(self.run_agent(ScriptedModel(answer("first"))).status, "completed")
        model = ScriptedModel(answer("second"))
        agent = Agent(model, self.executor, Store(self.store.root))
        self.events(agent.run("follow up"))
        self.assertEqual([m["content"] for m in model.requests[0][1:]], ["hello", "first", "follow up"])
        other = ScriptedModel(answer())
        Agent(other, self.executor, self.store).run("new", thread_id="other")
        self.assertEqual(len(other.requests[0]), 2)

    def test_demo_real_file_tools_and_events(self):
        (self.tools.root / "hello.txt").write_text("fixture", encoding="utf-8")
        result = self.run_agent(DemoModel())
        self.assertEqual(result.status, "completed")
        self.assertIn("fixture", (self.tools.root / "report.md").read_text(encoding="utf-8"))
        events = self.events(result)
        self.assertEqual(sum(e["event"] == "tool.end" and e["ok"] for e in events), 2)

    def test_tool_error_is_fed_back_and_recoverable(self):
        model = ScriptedModel(DemoModel.call("a", "read_file", {"path": "missing"}), answer("failed honestly"))
        result = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        self.assertFalse(json.loads(model.requests[1][-1]["content"])["ok"])
        self.assertTrue(any(e["event"] == "tool.end" and not e["ok"] for e in self.events(result)))

    def test_step_limit_does_not_save_partial_turn(self):
        model = ScriptedModel(DemoModel.call("a", "list_files", {"path": "."}))
        result = self.run_agent(model, max_steps=1)
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(self.store.load("default"), [])
        self.events(result)

    def test_model_corrects_bad_tool_arguments(self):
        model = ScriptedModel(DemoModel.call("bad", "list_files", {"path": 42}),
                              DemoModel.call("fixed", "list_files", {"path": "."}), answer())
        result = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        self.assertEqual(json.loads(model.requests[1][-1]["content"])["code"], "invalid_arguments")
        self.assertTrue(json.loads(model.requests[2][-1]["content"])["ok"])
        ends = [e for e in self.events(result) if e["event"] == "tool.end"]
        self.assertEqual([e["attempts"] for e in ends], [0, 1])

    def test_cancel_before_model(self):
        cancel = Event()
        cancel.set()
        model = ScriptedModel()
        result = Agent(model, self.executor, self.store).run("hello", cancel=cancel)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(model.requests, [])
        self.events(result)

    def test_cancel_after_model_prevents_tool(self):
        cancel = Event()
        model = ScriptedModel(DemoModel.call("a", "write_file", {"path": "bad", "content": "bad"}))
        def observe(event):
            if event["event"] == "model.end":
                cancel.set()
        result = Agent(model, self.executor, self.store).run("hello", cancel=cancel, observer=observe)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse((self.tools.root / "bad").exists())
        self.events(result)

    def test_deadline(self):
        with patch("baseline.core.agent.time") as clock:
            clock.monotonic.side_effect = [0, 2, 2]
            result = self.run_agent(ScriptedModel(), max_seconds=1)
        self.assertEqual(result.status, "timeout")
        self.events(result)

    def test_context_limit(self):
        result = self.run_agent(ScriptedModel(), max_context_chars=1)
        self.assertEqual(result.status, "context_limit")
        self.events(result)

    def test_truncated_response_never_executes(self):
        reply = DemoModel.call("a", "write_file", {"path": "bad", "content": "bad"})
        reply.finish_reason = "length"
        result = self.run_agent(ScriptedModel(reply))
        self.assertEqual(result.status, "model_terminated")
        self.assertFalse((self.tools.root / "bad").exists())
        self.events(result)

    def test_invalid_batch_no_partial_execution(self):
        reply = DemoModel.call("a", "write_file", {"path": "bad", "content": "bad"})
        reply.message["tool_calls"].append(reply.message["tool_calls"][0])
        result = self.run_agent(ScriptedModel(reply))
        self.assertEqual(result.status, "error")
        self.assertFalse((self.tools.root / "bad").exists())
        self.events(result)

    def test_error_and_keyboard_interrupt(self):
        result = self.run_agent(ScriptedModel(RuntimeError("secret should not appear")))
        self.assertEqual(result.status, "error")
        self.assertNotIn("secret should not appear", json.dumps(self.events(result)))
        with patch.object(ScriptedModel, "complete", side_effect=KeyboardInterrupt):
            result = self.run_agent(ScriptedModel())
        self.assertEqual(result.status, "cancelled")
        self.events(result)

    def test_usage_aggregation(self):
        first = DemoModel.call("a", "list_files", {"path": "."})
        first.usage = {"total_tokens": 10}
        last = answer()
        last.usage = {"total_tokens": 20}
        result = self.run_agent(ScriptedModel(first, last))
        self.assertEqual(self.events(result)[-1]["usage"], {"total_tokens": 30})

    def test_tool_boundaries(self):
        for name, arguments in [("unknown", "{}"), ("read_file", "not json"),
                                ("read_file", '{"path": 1}'),
                                ("read_file", '{"path": "../outside"}'),
                                ("read_file", '{"path": ".", "extra": "bad"}')]:
            with self.subTest(name=name, arguments=arguments):
                self.assertFalse(self.execute(name, arguments)["ok"])
        args = json.dumps({"path": "new.txt", "content": "original"})
        self.assertTrue(self.execute("write_file", args)["ok"])
        self.assertFalse(self.execute("write_file", args)["ok"])
        large = json.dumps({"path": "large", "content": "x" * 32001})
        self.assertFalse(self.execute("write_file", large)["ok"])
        (self.tools.root / "large").write_bytes(b"x" * 32001)
        self.assertFalse(self.execute("read_file", '{"path": "large"}')["ok"])


if __name__ == "__main__":
    unittest.main()
