"""Trusted development only: process-tree control does not isolate host filesystem/network."""
import sys
from pathlib import Path
from .contracts import SandboxLimits
from .process import clean_environment, managed_process

BOOTSTRAP = "import sys; code=sys.stdin.buffer.read().decode('utf-8'); exec(compile(code, '<agent>', 'exec'), {'__name__':'__main__'})"


class LocalSandbox:
    def __init__(self, workspace: Path, limits=SandboxLimits()):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.limits = limits

    async def run_python(self, code, context):
        if len(code.encode()) > self.limits.code_bytes:
            from .contracts import Execution
            return Execution("code_too_large")
        return await managed_process([sys.executable, "-I", "-u", "-c", BOOTSTRAP], cwd=self.workspace,
                                     env=clean_environment(self.workspace), data=code.encode(),
                                     limits=self.limits, context=context)
