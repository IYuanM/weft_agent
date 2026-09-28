"""Registry owns model schemas; the executor owns invocation."""
import math
import re
from .contracts import ToolSpec


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]):
        self._specs = {}
        for spec in specs:
            names = [name for name, _ in spec.parameters]
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec.name)
                    or spec.name in self._specs or len(names) != len(set(names))
                    or not math.isfinite(spec.timeout_seconds) or spec.timeout_seconds <= 0):
                raise ValueError("Invalid or duplicate tool specification")
            self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def schemas(self) -> list[dict]:
        return [spec.schema() for spec in self._specs.values()]
