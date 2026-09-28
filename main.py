"""Composition root: all runtime dependencies are assembled here."""

import argparse
import json
from pathlib import Path

from baseline.core.config import load_model_env
from baseline.models.chat import ChatModel, DemoModel
from baseline.core.agent import Agent
from baseline.persistence.store import Store
from baseline.tools.files import FileTools
from baseline.tools.executor import ToolExecutor
from baseline.tools.concurrency import Admission
from baseline.context.manager import ContextPolicy
from baseline.safety.loops import LoopPolicy
from baseline.sandbox.contracts import SandboxLimits
from baseline.sandbox.local import LocalSandbox
from baseline.sandbox.docker import DockerSandbox
from baseline.sandbox.tool import python_tool
from baseline.tools.registry import ToolRegistry
from baseline.skills.catalog import SkillCatalog

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Weft-Agent：可观察、可验证的单 Agent")
    parser.add_argument("message", nargs="?", default="读取 hello.txt，生成 report.md")
    parser.add_argument("--demo", action="store_true", help="离线脚本模型，无 API 调用")
    parser.add_argument("--thread", default="default")
    parser.add_argument("--workspace", type=Path, default=ROOT / "workspace")
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--context-window", type=int, default=24000, help="保守估算的总 token 预算")
    parser.add_argument("--context-keep-blocks", type=int, default=4)
    parser.add_argument("--loop-repetitions", type=int, default=3)
    parser.add_argument("--tool-concurrency", type=int, default=3, help="每批独立只读工具的并发上限")
    parser.add_argument("--tool-capacity", type=int, default=4, help="共享执行边界的总并发上限")
    parser.add_argument("--sandbox", choices=("off", "local", "docker"), default="off")
    parser.add_argument("--sandbox-image", default="python:3.12-slim")
    parser.add_argument("--sandbox-timeout", type=float, default=10)
    args = parser.parse_args()
    if not args.demo:
        try:
            load_model_env(ROOT / ".env")
        except ValueError as exc:
            parser.error(str(exc))
    tools = FileTools(args.workspace, workspace_lock=args.sandbox != "off")
    if args.demo:
        sample = tools.root / "hello.txt"
        if not sample.exists():
            sample.write_text("这是一个可以离线验证的 Agent 工具调用示例。\n", encoding="utf-8")
    try:
        model = DemoModel() if args.demo else ChatModel()
    except KeyError as exc:
        parser.error(f"缺少配置 {exc.args[0]}，请填写 Weft-Agent/.env 或使用 --demo")
    registry = tools.registry()
    if args.sandbox != "off":
        limits = SandboxLimits(timeout_seconds=args.sandbox_timeout)
        sandbox = (LocalSandbox(tools.root, limits) if args.sandbox == "local"
                   else DockerSandbox(tools.root, limits, image=args.sandbox_image))
        if args.sandbox == "docker" and not sandbox.executable:
            parser.error("未找到 Docker CLI，请安装并启动 Docker；不会回退到本地执行")
        registry = ToolRegistry([registry.get(name) for name in registry.names()] + [python_tool(sandbox, tools.root)])
    skills = SkillCatalog(ROOT / "skills")
    registry = ToolRegistry([registry.get(name) for name in registry.names()] + [skills.tool()])
    executor = ToolExecutor(registry, admission=Admission(args.tool_capacity), max_parallel=args.tool_concurrency)
    agent = Agent(model, executor, Store(args.state_dir), max_steps=args.max_steps, max_seconds=args.max_seconds,
                  context_policy=ContextPolicy(window_tokens=args.context_window, keep_blocks=args.context_keep_blocks),
                  loop_policy=LoopPolicy(repetitions=args.loop_repetitions), skills=skills)
    result = agent.run(args.message, thread_id=args.thread,
                       observer=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    print(f"\nstatus={result.status}  thread={result.thread_id}\nevents={result.event_path}")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
