"""Opt-in paid API smoke test. Run explicitly: python smoke_live.py."""

import json
import argparse
import tempfile
from pathlib import Path

from baseline.core.config import load_model_env
from baseline.models.chat import ChatModel
from baseline.core.agent import Agent
from baseline.persistence.store import Store
from baseline.tools.files import FileTools
from baseline.tools.executor import ToolExecutor
from baseline.context.manager import ContextPolicy

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compress", action="store_true", help="另外验证真实模型摘要和压缩后工具调用")
    args = parser.parse_args()
    load_model_env(ROOT / ".env")
    # Only this synthetic fixture is exposed to the model's tools.
    with tempfile.TemporaryDirectory(prefix="baseline-smoke-") as directory:
        tools = FileTools(Path(directory))
        (tools.root / "probe.txt").write_text("verification_code=BASELINE_OK_742\n", encoding="utf-8")
        store = Store(ROOT / ".state" / "live-smoke")
        thread_id = Path(directory).name
        if args.compress:
            store.save(thread_id, [{"role": "user" if i % 2 == 0 else "assistant",
                                    "content": "Synthetic historical data, no outstanding actions. " + "example " * 110}
                                   for i in range(8)])
        policy = ContextPolicy(window_tokens=10000, keep_blocks=2, summary_max_chars=1200) if args.compress else ContextPolicy()
        result = Agent(ChatModel(), ToolExecutor(tools.registry()), store, max_steps=4, context_policy=policy).run(
            "必须调用 read_file 读取 probe.txt，只回复文件里的 verification_code，不要猜测。",
            thread_id=thread_id,
            observer=lambda item: print(json.dumps(item, ensure_ascii=False), flush=True),
        )
        events = [json.loads(line) for line in Path(result.event_path).read_text(encoding="utf-8").splitlines()]
        used_tool = any(e["event"] == "tool.end" and e["tool"] == "read_file" and e["ok"] for e in events)
        passed = result.status == "completed" and used_tool and "BASELINE_OK_742" in result.answer
        if args.compress:
            passed = passed and any(e["event"] == "context.compaction.end" and e["ok"] for e in events)
        print("LIVE_SMOKE=" + ("PASS" if passed else "FAIL"))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
