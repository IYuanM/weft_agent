"""Bounded pipe capture and process-tree ownership. Never execute through a shell."""
import asyncio
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from .contracts import Execution, SandboxLimits
from ..tools.concurrency import drain


def clean_environment(workspace: Path):
    env = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"}
    for key in ("SystemRoot", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    temporary = workspace / ".sandbox-tmp"
    temporary.mkdir(exist_ok=True)
    env.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
    return env


def run_process(argv, *, cwd, env, data, limits, context, stop, own_tree=True):
    if stop.is_set() or context.stop_reason():
        return Execution(context.stop_reason() or "cancelled")
    process = None
    job = None
    threads = []
    output = [bytearray(), bytearray()]
    lock = threading.Lock()
    exceeded = threading.Event()
    status = "ok"
    cleanup_ok = True
    deadline = min(context.deadline, time.monotonic() + limits.timeout_seconds)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        if os.name == "nt" and own_tree:
            from .windows_job import Job
            # Local Python waits for stdin before executing any supplied code.
            job = Job(process.pid, memory_mb=limits.memory_mb, pids=limits.pids)

        def capture(stream, target):
            try:
                while chunk := stream.read1(4096):
                    with lock:
                        remaining = limits.output_bytes - sum(map(len, output))
                        target.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            exceeded.set()
            finally:
                stream.close()

        def feed():
            try:
                process.stdin.write(data)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        for function, args in [(capture, (process.stdout, output[0])), (capture, (process.stderr, output[1])), (feed, ())]:
            thread = threading.Thread(target=function, args=args, daemon=True)
            thread.start()
            threads.append(thread)
        while process.poll() is None:
            reason = context.stop_reason()
            if stop.is_set() or reason:
                status = reason or "cancelled"
                break
            if exceeded.is_set():
                status = "output_limit"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                break
            time.sleep(0.01)
    except FileNotFoundError:
        status = "sandbox_unavailable"
    except OSError:
        status = "sandbox_start_failed"
    finally:
        if process is not None:
            # Even a normally exiting parent must not leave descendants behind.
            if job:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_ok = False
            for thread in threads:
                thread.join(timeout=2)
                cleanup_ok = cleanup_ok and not thread.is_alive()
            if not threads:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
    if not cleanup_ok:
        status = "cleanup_failed"
    elif exceeded.is_set():
        status = "output_limit"
    elif status == "ok" and process.returncode != 0:
        status = "execution_failed"
    return Execution(status, process.returncode if process else None,
                     output[0].decode("utf-8", errors="replace"), output[1].decode("utf-8", errors="replace"),
                     exceeded.is_set(), cleanup_ok)


async def managed_process(*args, **kwargs):
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(run_process, *args, **kwargs, stop=stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await drain(task)
        if not task.cancelled():
            task.exception()
        raise
