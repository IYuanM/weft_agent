import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch

from baseline.core.run_context import RunContext
from baseline.sandbox.contracts import Execution, SandboxLimits
from baseline.sandbox.local import LocalSandbox
from baseline.sandbox.docker import DockerSandbox
from baseline.sandbox.tool import python_tool
from baseline.tools.files import FileTools
from baseline.tools.executor import ToolExecutor
from baseline.tools.registry import ToolRegistry
from baseline.models.chat import DemoModel


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def context(self):
        return RunContext("sandbox", "test", Event(), time.monotonic() + 10)

    async def test_local_output_files_and_environment(self):
        with patch.dict(os.environ, {"MODEL_API_KEY": "must-not-inherit", "CUSTOM_SECRET": "hidden"}):
            result = await LocalSandbox(self.root).run_python(
                "import os; from pathlib import Path; print(os.getenv('MODEL_API_KEY')); "
                "print(os.getenv('CUSTOM_SECRET')); Path('result.txt').write_text('ok'); print('done')", self.context())
        self.assertEqual(result.code, "ok", result)
        self.assertEqual(result.stdout.splitlines(), ["None", "None", "done"])
        self.assertEqual((self.root / "result.txt").read_text(), "ok")
        self.assertTrue(result.cleanup_ok)

    async def test_nonzero_exit_and_stderr(self):
        result = await LocalSandbox(self.root).run_python("import sys; print('problem', file=sys.stderr); sys.exit(7)", self.context())
        self.assertEqual(result.code, "execution_failed")
        self.assertEqual(result.exit_code, 7)
        self.assertIn("problem", result.stderr)

    async def test_timeout_kills_process(self):
        started = time.monotonic()
        result = await LocalSandbox(self.root, SandboxLimits(timeout_seconds=0.2)).run_python("import time; time.sleep(20)", self.context())
        self.assertEqual(result.code, "timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result.cleanup_ok)

    async def test_output_flood_is_bounded_and_terminated(self):
        result = await LocalSandbox(self.root, SandboxLimits(output_bytes=1024)).run_python("while True: print('x' * 1000)", self.context())
        self.assertEqual(result.code, "output_limit")
        self.assertLessEqual(len(result.stdout.encode()) + len(result.stderr.encode()), 1024)
        self.assertTrue(result.truncated)

    async def test_event_cancellation(self):
        context = self.context()
        task = asyncio.create_task(LocalSandbox(self.root).run_python("import time; time.sleep(20)", context))
        await asyncio.sleep(0.1)
        context.cancel.set()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result.code, "cancelled")
        self.assertTrue(result.cleanup_ok)

    async def test_external_cancellation_reaps_before_return(self):
        task = asyncio.create_task(LocalSandbox(self.root).run_python(
            "import time; from pathlib import Path; Path('started').touch(); time.sleep(1); Path('orphan').touch()", self.context()))
        for _ in range(100):
            if (self.root / "started").exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue((self.root / "started").exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        await asyncio.sleep(1.1)
        self.assertFalse((self.root / "orphan").exists())

    async def test_descendant_killed_after_parent_normal_exit(self):
        child = "import time; from pathlib import Path; time.sleep(0.7); Path('child-survived').touch()"
        source = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('parent finished')"
        result = await LocalSandbox(self.root).run_python(source, self.context())
        self.assertEqual(result.code, "ok", result)
        await asyncio.sleep(0.9)
        self.assertFalse((self.root / "child-survived").exists())

    async def test_bad_code_size_and_pre_cancel_no_execution(self):
        sandbox = LocalSandbox(self.root, SandboxLimits(code_bytes=20))
        self.assertEqual((await sandbox.run_python("x" * 21, self.context())).code, "code_too_large")
        context = self.context()
        context.cancel.set()
        self.assertEqual((await sandbox.run_python("print('x')", context)).code, "cancelled")

    async def test_shared_workspace_resource_for_script_and_files(self):
        files = FileTools(self.root, workspace_lock=True)
        spec = python_tool(LocalSandbox(self.root), self.root)
        context = self.context()
        self.assertEqual(files.authorize({"path": "a"}, context), spec.authorize({"code": "pass"}, context))
        result = await spec.handler({"code": "print(42)"}, context)
        self.assertTrue(result.ok)
        self.assertEqual(result.effect, "confirmed")

    def test_docker_restrictions_are_host_owned(self):
        sandbox = DockerSandbox(self.root, executable="docker")
        command = sandbox.command("agent-baseline-test")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pull=never", "--log-driver=none"):
            self.assertIn(flag, command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("MODEL_API_KEY", " ".join(command))
        self.assertEqual(command[1], "create")

    async def test_docker_create_start_cleanup_without_daemon(self):
        sandbox = DockerSandbox(self.root, executable="docker")
        with patch("baseline.sandbox.docker.managed_process", new_callable=AsyncMock) as run, patch.object(sandbox, "_remove", return_value=True) as remove:
            run.side_effect = [Execution("ok", 0, "container-id"), Execution("ok", 0, "42\n")]
            result = await sandbox.run_python("print(42)", self.context())
            self.assertEqual(result.stdout, "42\n")
            self.assertEqual(run.call_args_list[1].args[0][1:4], ["start", "-a", "-i"])
            self.assertEqual(run.call_args_list[1].kwargs["data"], b"print(42)")
            remove.assert_called_once()

    async def test_docker_cleanup_failure_quarantines(self):
        sandbox = DockerSandbox(self.root, executable="docker")
        with patch("baseline.sandbox.docker.managed_process", new_callable=AsyncMock, return_value=Execution("execution_failed", 125)), patch.object(sandbox, "_remove", return_value=False):
            self.assertEqual((await sandbox.run_python("pass", self.context())).code, "cleanup_failed")
            self.assertEqual((await sandbox.run_python("pass", self.context())).code, "sandbox_quarantined")

    async def test_docker_missing_never_falls_back_to_host(self):
        with patch("baseline.sandbox.docker.shutil.which", return_value=None):
            result = await DockerSandbox(self.root).run_python("pass", self.context())
        self.assertEqual(result.code, "sandbox_unavailable")

    async def test_executor_timeout_reaps_before_lock_release(self):
        spec = python_tool(LocalSandbox(self.root, SandboxLimits(timeout_seconds=0.15)), self.root)
        executor = ToolExecutor(ToolRegistry([spec]))
        events = []
        context = self.context()
        result = await executor.aexecute("run_python", '{"code":"import time; time.sleep(20)"}',
                                         context=context, tool_call_id="timeout", step=1,
                                         emit=lambda event, **data: events.append({"event": event, **data}))
        self.assertIn(result.code, {"timeout", "cancelled"})
        self.assertEqual(executor.admission.active, 0)
        self.assertEqual(executor.admission.locks, {})
        self.assertEqual(sum(e["event"] == "tool.end" for e in events), 1)
        self.assertEqual(result.effect, "unknown")

    async def test_script_then_file_read_through_executor(self):
        files = FileTools(self.root, workspace_lock=True)
        registry = files.registry()
        executor = ToolExecutor(ToolRegistry([registry.get(name) for name in registry.names()]
                                            + [python_tool(LocalSandbox(self.root), self.root)]))
        calls = [DemoModel.call("script", "run_python", {"code": "from pathlib import Path; Path('created.txt').write_text('42')"}).message["tool_calls"][0],
                 DemoModel.call("read", "read_file", {"path": "created.txt"}).message["tool_calls"][0]]
        results = await executor.execute_batch(calls, context=self.context(), step=1, emit=lambda *args, **kw: None)
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(results[1].content["text"], "42")

    async def test_create_timeout_quarantines_even_if_remove_says_absent(self):
        sandbox = DockerSandbox(self.root, executable="docker")
        with patch("baseline.sandbox.docker.managed_process", new_callable=AsyncMock, return_value=Execution("timeout")), patch.object(sandbox, "_remove", return_value=True):
            result = await sandbox.run_python("pass", self.context())
            self.assertEqual(result.code, "cleanup_failed")
            self.assertTrue(sandbox.quarantined)
