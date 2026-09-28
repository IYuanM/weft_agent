"""Detect no-progress patterns independently of model step limits and compaction."""
import hashlib
import json
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LoopPolicy:
    repetitions: int = 3
    max_period: int = 2

    def __post_init__(self):
        if not 2 <= self.repetitions <= 10 or not 1 <= self.max_period <= 8:
            raise ValueError("Invalid loop policy")


class LoopDetector:
    def __init__(self, policy=LoopPolicy()):
        self.policy = policy
        self.records = deque(maxlen=policy.repetitions * policy.max_period)
        self.warned = False

    def observe(self, name, arguments, result, *, allow_repeated_reads=False):
        if allow_repeated_reads and result.ok:
            self.records.clear()
            return
        try:
            args = json.loads(arguments)
        except ValueError:
            args = arguments
        # Failure codes ignore changing arguments: repeated unsuccessful repair is still no progress.
        value = {"tool": name, "code": result.code, "effect": result.effect}
        if result.ok:
            value.update(arguments=args, content=result.content, artifacts=result.artifacts)
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.records.append(fingerprint)

    def check(self):
        records = list(self.records)
        for period in range(1, self.policy.max_period + 1):
            size = period * self.policy.repetitions
            if len(records) >= size and records[-size:] == records[-period:] * self.policy.repetitions:
                action = "stop" if self.warned else "warn"
                self.warned = True
                return action, period
        return None, None


WARNING = {"role": "user", "content": "运行时检测到重复且没有可观察进展的工具调用。"
           "请更换方法或如实解释阻碍，不要继续重复相同失败或已完成的写操作。继续循环将终止本轮。"}
