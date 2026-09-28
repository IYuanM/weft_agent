"""Small literal .env reader: process environment takes precedence."""

import os
from pathlib import Path

MODEL_KEYS = {"MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"}


def load_model_env(path: Path) -> None:
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in MODEL_KEYS or not value:
            raise ValueError(f"Invalid model configuration at line {number}")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed configuration quote at line {number}")
            value = value[1:-1]
        if not value:
            raise ValueError(f"Empty model configuration at line {number}")
        os.environ.setdefault(key, value)
