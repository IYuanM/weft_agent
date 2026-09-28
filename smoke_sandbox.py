"""Explicit real sandbox acceptance; no model API or implicit image downloads."""
import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event

from baseline.core.run_context import RunContext
from baseline.sandbox.contracts import SandboxLimits
from baseline.sandbox.local import LocalSandbox
from baseline.sandbox.docker import DockerSandbox


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("local", "docker"), required=True)
    parser.add_argument("--image", default="python:3.12-slim")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="baseline-sandbox-") as directory:
        root = Path(directory)
        # Allow the container's non-root user to write only this temporary fixture directory.
        root.chmod(0o777)
        sandbox = (LocalSandbox(root) if args.provider == "local"
                   else DockerSandbox(root, image=args.image))
        def context():
            return RunContext("smoke", "sandbox", Event(), time.monotonic() + 30)
        code = "import os; from pathlib import Path; assert 'MODEL_API_KEY' not in os.environ; Path('proof').write_text('ok'); print('SANDBOX_OK')"
        if args.provider == "docker":
            code += "\nassert os.getuid() != 0\ntry:\n Path('/outside-proof').write_text('bad')\nexcept OSError:\n print('ROOT_READ_ONLY')\nelse:\n raise AssertionError('root writable')"
        result = await sandbox.run_python(code, context())
        print(json.dumps(asdict(result), ensure_ascii=False))
        passed = result.code == "ok" and "SANDBOX_OK" in result.stdout and (root / "proof").read_text() == "ok"
        if not passed:
            return 1
        sandbox.limits = SandboxLimits(timeout_seconds=1)
        timed = await sandbox.run_python("import time; time.sleep(20)", context())
        print(json.dumps(asdict(timed), ensure_ascii=False))
        passed = timed.code == "timeout" and timed.cleanup_ok
        print("SANDBOX_SMOKE=" + ("PASS" if passed else "FAIL"))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
