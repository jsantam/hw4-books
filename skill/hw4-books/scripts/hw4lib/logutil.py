"""Structured JSONL logging with secret redaction. Every log line passes through redact()."""
import json
import re
import sys
import time
from pathlib import Path

_SECRETS: list[str] = []
_PATTERNS = [
    re.compile(r"\b\d{4,6}~[A-Za-z0-9]{20,}\b"),          # Canvas token shape
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),             # Anthropic key
    re.compile(r"(?i)bearer\s+[A-Za-z0-9~._\-]{10,}"),
]


def register_secret(value: str) -> None:
    if value and len(value) >= 8:
        _SECRETS.append(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "[REDACTED]")
    for p in _PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


class RunLog:
    def __init__(self, state_dir: Path, run_id: str):
        self.run_id = run_id
        logs = state_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.path = logs / "agent.jsonl"

    def event(self, _event: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": self.run_id, "event": _event, **fields}
        line = redact(json.dumps(rec, default=str, ensure_ascii=False))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, file=sys.stderr)
