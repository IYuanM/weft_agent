"""Model boundary. The runtime knows only complete(messages, tools, timeout)."""

import json
import os
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    """Only controlled metadata may be recorded; never log response bodies."""

    def __init__(self, code: str, http_status: int | None = None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


@dataclass
class Reply:
    message: dict
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)


class ChatModel:
    def __init__(self):
        self.name = os.environ["MODEL_NAME"]
        self.url = os.environ["MODEL_BASE_URL"].rstrip("/") + "/chat/completions"
        self.key = os.environ["MODEL_API_KEY"]

    def complete(self, messages: list[dict], tools: list[dict], timeout: float) -> Reply:
        payload = {"model": self.name, "messages": messages, "tools": tools,
                   "stream": False, "max_tokens": 2048}
        if not tools:
            payload.pop("tools")
        request = Request(self.url, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise RuntimeError("Model response exceeds size limit")
            body = json.loads(data)
            choice = body["choices"][0]
            message = choice["message"]
            # Preserve provider reasoning_content when required for subsequent tool turns.
            message = {k: v for k, v in message.items()
                       if k in {"role", "content", "tool_calls", "reasoning_content"}}
            return Reply(message, choice["finish_reason"], body.get("usage") or {})
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ModelError("http_error", status) from None
        except URLError:
            raise ModelError("network_error") from None
        except TimeoutError:
            raise ModelError("request_timeout") from None


class DemoModel:
    """Deterministic demonstration, explicitly NOT an LLM."""
    name = "offline-demo (scripted)"

    def complete(self, messages, tools, timeout):
        last_user = max(i for i, message in enumerate(messages) if message["role"] == "user")
        results = [m for m in messages[last_user + 1:] if m["role"] == "tool"]
        if not results:
            return self.call("demo-read", "read_file", {"path": "hello.txt"})
        if len(results) == 1:
            result = json.loads(results[0]["content"])
            if not result["ok"]:
                return Reply({"role": "assistant", "content": "读取失败，请查看 tool.end 事件。"})
            return self.call("demo-write", "write_file", {
                "path": "report.md", "content": "# 离线演示报告\n\n" + result["content"]["text"],
            })
        success = json.loads(results[-1]["content"])["ok"]
        text = "已读取 hello.txt 并生成 report.md。" if success else "文件写入失败（演示不会覆盖已有报告），请查看事件。"
        return Reply({"role": "assistant", "content": text})

    @staticmethod
    def call(call_id, name, args):
        return Reply({"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}, "tool_calls")
