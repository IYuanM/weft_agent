"""Disposable containers. Never fall back to host execution or implicitly pull images."""
import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from .contracts import Execution, SandboxLimits
from .local import BOOTSTRAP
from .process import clean_environment, managed_process
from ..tools.concurrency import drain


class DockerSandbox:
    def __init__(self, workspace: Path, limits=SandboxLimits(), image="python:3.12-slim", executable=None):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        if any(char in str(self.workspace) for char in (",", "\n", "\r")):
            raise ValueError("Workspace cannot be encoded as a Docker bind mount")
        if not image or image.startswith("-") or any(char.isspace() for char in image):
            raise ValueError("Invalid image reference")
        self.limits, self.image = limits, image
        self.executable = executable or shutil.which("docker")
        self.quarantined = False

    def environment(self):
        # Docker CLI may need its host-side config directory. These are NOT container env flags.
        env = clean_environment(self.workspace)
        for key in ("HOME", "USERPROFILE"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def command(self, name):
        uid = str(os.getuid()) if hasattr(os, "getuid") and os.getuid() != 0 else "1000"
        return [self.executable, "create", "--pull=never", "--name", name, "--label", "agent-baseline.sandbox=true",
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                "--pids-limit", str(self.limits.pids), "--memory", f"{self.limits.memory_mb}m",
                "--memory-swap", f"{self.limits.memory_mb}m", "--cpus", str(self.limits.cpus),
                "--user", uid + ":" + uid, "--init", "--log-driver=none", "--workdir", "/workspace",
                "--mount", f"type=bind,source={self.workspace},target=/workspace",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m", "--entrypoint", "python", "-i",
                self.image, "-I", "-u", "-c", BOOTSTRAP]

    def _remove(self, name):
        # No shell, no user-controlled target, separate cleanup deadline.
        try:
            result = subprocess.run([self.executable, "rm", "-f", name], env=self.environment(),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return result.returncode == 0 or b"No such container" in result.stderr
        except (OSError, subprocess.TimeoutExpired):
            return False

    async def run_python(self, code, context):
        if self.quarantined:
            return Execution("sandbox_quarantined", cleanup_ok=False)
        if not self.executable:
            return Execution("sandbox_unavailable")
        if len(code.encode()) > self.limits.code_bytes:
            return Execution("code_too_large")
        name = "agent-baseline-" + uuid.uuid4().hex
        result = None
        creating = True
        try:
            result = await managed_process(self.command(name), cwd=self.workspace,
                                           env=self.environment(), data=b"",
                                           limits=self.limits, context=context, own_tree=False)
            if result.code == "ok":
                creating = False
                result = await managed_process([self.executable, "start", "-a", "-i", name], cwd=self.workspace,
                                               env=self.environment(), data=code.encode(),
                                               limits=self.limits, context=context, own_tree=False)
        finally:
            cleanup = asyncio.create_task(asyncio.to_thread(self._remove, name))
            try:
                cleaned = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await drain(cleanup)
                cleaned = cleanup.result()
                self.quarantined = not cleaned or creating
                raise
            # A timed-out create request may still be in flight in the daemon.
            if creating and (result is None or result.code in {"timeout", "cancelled"}):
                cleaned = False
            self.quarantined = not cleaned
        if not cleaned:
            return Execution("cleanup_failed", result.exit_code, result.stdout, result.stderr,
                             result.truncated, False)
        return result
