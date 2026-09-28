"""Tool adapter; command execution shares the workspace lock with file tools."""
import os
from pathlib import Path
from .contracts import Sandbox
from ..tools.contracts import ToolSpec, ToolResult, ToolFailure


def python_tool(sandbox: Sandbox, workspace: Path):
    resource = os.path.normcase(str(workspace.resolve()))

    def authorize(args, context):
        if len(args["code"].encode()) > sandbox.limits.code_bytes:
            raise ToolFailure("code_too_large")
        return resource

    async def execute(args, context):
        result = await sandbox.run_python(args["code"], context)
        effect = "confirmed" if result.code == "ok" else "unknown"
        if result.code in {"code_too_large", "sandbox_unavailable", "sandbox_start_failed", "sandbox_quarantined"}:
            effect = "none"
        return ToolResult(result.code == "ok", result.code, content={
            "exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr,
            "truncated": result.truncated, "cleanup_ok": result.cleanup_ok,
        }, effect=effect)

    return ToolSpec("run_python", "Execute Python in the configured sandbox. Working directory is the workspace. "
                    "May create/change files. Inspect stdout, stderr and exit_code; do not repeat uncertain writes.",
                    (("code", "Python source code"),), execute, authorize, read_only=False, idempotent=False,
                    timeout_seconds=sandbox.limits.timeout_seconds)
