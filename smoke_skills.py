"""Two end-to-end skill scenarios. --live uses the configured paid model API."""
import argparse
import json
import shutil
import uuid
from pathlib import Path

from baseline.core.agent import Agent
from baseline.core.config import load_model_env
from baseline.models.chat import ChatModel, DemoModel, Reply
from baseline.persistence.store import Store
from baseline.skills.catalog import SkillCatalog
from baseline.tools.executor import ToolExecutor
from baseline.tools.files import FileTools
from baseline.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parent
SCENARIOS = [
    {"skill": "code-documentation", "input": "calculator.py", "output": "outputs/README.md",
     "prompt": "为 calculator.py 生成中文使用文档 outputs/README.md，覆盖函数参数、默认值、返回值、异常，"
               "给出 total_price(19.9, 3, 0.1) 的结果示例。请按可用技能的流程完成并回读检查文件。",
     "tokens": ["total_price", "discount", "ValueError", "53.73"]},
    {"skill": "newsletter-generation", "input": "notes.md", "output": "outputs/newsletter.md",
     "prompt": "根据 notes.md 写一份面向开发者的中文简报，保存 outputs/newsletter.md，保留两条来源 URL、日期和数字。"
               "这是合成测试材料，不要当成真实新闻。请按可用技能的流程完成并回读检查文件。",
     "tokens": ["120", "90", "77", "80", "2026-09-20", "2026-09-21",
                "https://example.invalid/weft/parser", "https://example.invalid/weft/tests"]},
]


class OfflineSkillModel:
    """Scripted protocol fixture, not an LLM or proof of document quality."""
    name = "offline-skill-fixture"

    def __init__(self, scenario):
        self.scenario = scenario
        self.step = 0

    def complete(self, messages, tools, timeout):
        self.step += 1
        s = self.scenario
        if self.step == 1:
            return DemoModel.call("load", "load_skill", {"name": s["skill"]})
        if self.step == 2:
            return DemoModel.call("source", "read_file", {"path": s["input"]})
        if self.step == 3:
            return DemoModel.call("write", "write_file", {"path": s["output"], "content": "# Offline fixture\n\n" + "\n".join(s["tokens"])})
        if self.step == 4:
            return DemoModel.call("verify", "read_file", {"path": s["output"]})
        return Reply({"role": "assistant", "content": s["output"]})


def verify(messages, workspace, scenario):
    results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
    calls = [(call["function"]["name"], json.loads(call["function"]["arguments"]), results.get(call["id"], {}))
             for message in messages for call in message.get("tool_calls", [])]
    def index(name, key, value):
        return next((i for i, (n, args, result) in enumerate(calls)
                     if n == name and args.get(key) == value and result.get("ok")), -1)
    load = index("load_skill", "name", scenario["skill"])
    source = index("read_file", "path", scenario["input"])
    write = index("write_file", "path", scenario["output"])
    readback = index("read_file", "path", scenario["output"])
    path = workspace / scenario["output"]
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"ordered_workflow": 0 <= load < source < write < readback,
            "artifact_exists": path.is_file(),
            "required_content": all(token in text for token in scenario["tokens"])}


def run_scenario(scenario, *, live, base, skills, store, verbose=True):
    workspace = base / scenario["skill"]
    workspace.mkdir(parents=True)
    shutil.copyfile(ROOT / "examples" / "skills_inputs" / scenario["input"], workspace / scenario["input"])
    files = FileTools(workspace).registry()
    executor = ToolExecutor(ToolRegistry([files.get(n) for n in files.names()] + [skills.tool()]))
    model = ChatModel() if live else OfflineSkillModel(scenario)
    thread = base.name + "-" + scenario["skill"]
    result = Agent(model, executor, store, skills=skills, max_steps=12, max_seconds=180).run(
        scenario["prompt"], thread_id=thread,
        observer=(lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)) if verbose else None)
    checks = verify(store.load(thread), workspace, scenario)
    return {"skill": scenario["skill"], "status": result.status,
            "passed": result.status == "completed" and all(checks.values()), "checks": checks,
            "artifact": str(workspace / scenario["output"]), "events": result.event_path}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="调用真实模型，会产生 API 用量")
    args = parser.parse_args()
    if args.live:
        load_model_env(ROOT / ".env")
    base = ROOT / "workspace" / ("skill-smoke-" + uuid.uuid4().hex[:10])
    store = Store(ROOT / ".state" / "skill-smoke")
    skills = SkillCatalog(ROOT / "skills")
    reports = [run_scenario(s, live=args.live, base=base, skills=skills, store=store) for s in SCENARIOS]
    report = {"mode": "live" if args.live else "offline-scripted", "results": reports}
    path = store.root / "last_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(result["passed"] for result in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
