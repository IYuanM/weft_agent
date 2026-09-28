import json
import time
import unittest
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

from baseline.tools.contracts import RunContext, ToolFailure, ToolResult, ToolSpec
from baseline.tools.executor import ToolExecutor
from baseline.tools.registry import ToolRegistry


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.handler = Mock(return_value=ToolResult(True, content={"text": "ok"}))
        self.authorize = Mock(return_value="workspace/file")
        self.spec = ToolSpec("sample", "test", (("path", "path"),), self.handler, self.authorize)
        self.context = RunContext("thread", "run", Event(), time.monotonic() + 60)
        self.events = []

    def execute(self, args='{"path":"file"}', *, spec=None, **limits):
        executor = ToolExecutor(ToolRegistry([spec or self.spec]), retry_delay=0, **limits)
        result = executor.execute("sample", args, context=self.context, tool_call_id="call", step=1,
                                  emit=lambda name, **data: self.events.append({"event": name, **data}))
        self.assertEqual(sum(e["event"] == "tool.start" for e in self.events),
                         sum(e["event"] == "tool.end" for e in self.events))
        self.assertEqual(self.events[-1]["code"], result.code)
        return result

    def test_invalid_arguments_never_reach_authorization_or_handler(self):
        for args in ['bad', '[]', '{"path":2}', '{}', '{"path":"x","extra":"x"}',
                     '{"path":"x","path":"y"}']:
            self.assertEqual(self.execute(args).code, "invalid_arguments")
        self.handler.assert_not_called()
        self.authorize.assert_not_called()

    def test_authorization_denied(self):
        self.authorize.side_effect = ToolFailure("permission_denied")
        self.assertEqual(self.execute().code, "permission_denied")
        self.handler.assert_not_called()
        self.assertEqual(self.events[-1]["attempts"], 0)

    def test_transient_read_recovers(self):
        self.handler.side_effect = [ConnectionError("private"), ToolResult(True)]
        self.assertTrue(self.execute().ok)
        self.assertEqual(self.handler.call_count, 2)
        self.assertEqual(self.authorize.call_count, 3)  # resource resolution + per-attempt recheck
        self.assertEqual(sum(e["event"] == "tool.retry" for e in self.events), 1)
        self.assertNotIn("private", json.dumps(self.events))

    def test_retry_limit(self):
        self.handler.side_effect = TimeoutError()
        result = self.execute(max_attempts=3)
        self.assertEqual(result.code, "transient_error")
        self.assertEqual(self.handler.call_count, 3)

    def test_business_failure_never_retries(self):
        self.handler.side_effect = FileNotFoundError("private")
        self.assertEqual(self.execute().code, "not_found")
        self.assertEqual(self.handler.call_count, 1)

    def test_non_idempotent_read_never_retries(self):
        self.handler.side_effect = ConnectionError()
        self.assertFalse(self.execute(spec=replace(self.spec, idempotent=False)).retryable)
        self.assertEqual(self.handler.call_count, 1)

    def test_uncertain_write_blocks_reexecution_for_resource(self):
        self.handler.side_effect = TimeoutError("write may have committed")
        spec = replace(self.spec, read_only=False, idempotent=False)
        first = self.execute(spec=spec)
        self.assertEqual(first.effect, "unknown")
        self.assertFalse(first.retryable)
        second = self.execute(spec=spec)
        self.assertEqual(second.code, "previous_effect_unknown")
        self.assertEqual(self.handler.call_count, 1)

    def test_write_even_if_idempotent_never_automatically_retries(self):
        self.handler.side_effect = ToolFailure("transient_error", retryable=True)
        self.execute(spec=replace(self.spec, read_only=False, idempotent=True))
        self.assertEqual(self.handler.call_count, 1)

    def test_budget_and_cancel(self):
        self.context.max_tool_calls = 0
        self.assertEqual(self.execute().code, "tool_budget_exceeded")
        self.context.cancel.set()
        self.assertEqual(self.execute().code, "cancelled")
        self.handler.assert_not_called()

    def test_cancellation_during_retry_backoff(self):
        self.handler.side_effect = ConnectionError()
        executor = ToolExecutor(ToolRegistry([self.spec]), retry_delay=0)
        def emit(name, **data):
            if name == "tool.retry":
                self.context.cancel.set()
        result = executor.execute("sample", '{"path":"x"}', context=self.context,
                                  tool_call_id="x", step=1, emit=emit)
        self.assertEqual(result.code, "cancelled")
        self.assertEqual(self.handler.call_count, 1)

    def test_expired_deadline_does_not_execute(self):
        self.context.deadline = time.monotonic() - 1
        self.assertEqual(self.execute().code, "timeout")
        self.handler.assert_not_called()

    def test_write_confirmation_survives_elapsed_run_deadline(self):
        def write(args, context):
            context.deadline = time.monotonic() - 1
            return ToolResult(True, artifacts=("file",), effect="confirmed")
        result = self.execute(spec=replace(self.spec, handler=write, read_only=False))
        self.assertEqual(result.code, "timeout")
        self.assertEqual(result.effect, "confirmed")
        self.assertEqual(result.artifacts, ("file",))

    def test_result_limit(self):
        self.handler.return_value = ToolResult(True, content={"text": "x" * 1000})
        self.assertEqual(self.execute(max_result_chars=256).code, "result_too_large")

    def test_invalid_handler_result_becomes_safe_error(self):
        self.handler.return_value = {"secret": "private-value"}
        self.assertEqual(self.execute().code, "internal_error")
        self.assertNotIn("private-value", json.dumps(self.events))

    def test_registry_duplicates_and_schema_copy(self):
        with self.assertRaises(ValueError):
            ToolRegistry([self.spec, self.spec])
        registry = ToolRegistry([self.spec])
        registry.schemas()[0]["function"]["parameters"]["required"].clear()
        self.assertEqual(registry.schemas()[0]["function"]["parameters"]["required"], ["path"])

    def test_confirmed_write_is_not_executed_twice(self):
        self.handler.return_value = ToolResult(True, artifacts=("file",), effect="confirmed")
        spec = replace(self.spec, read_only=False, idempotent=False)
        self.assertTrue(self.execute(spec=spec).ok)
        result = self.execute(spec=spec)
        self.assertEqual(result.code, "duplicate_write")
        self.assertEqual(result.artifacts, ("file",))
        self.assertEqual(self.handler.call_count, 1)
