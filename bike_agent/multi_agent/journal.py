"""Append-only, per-run audit. Facts survive errors and interrupted execution."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encoded(value: Any) -> str:
    def default(item):
        if hasattr(item, "item"):
            return item.item()
        if hasattr(item, "isoformat"):
            return item.isoformat()
        raise TypeError(type(item).__name__)
    text = json.dumps(value, ensure_ascii=False, default=default, allow_nan=False)
    key = os.environ.get("DEEPSEEK_API_KEY")
    return text.replace(key, "[REDACTED]") if key else text


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(encoded(value) + "\n", encoding="utf-8")
    tmp.replace(path)


class Journal:
    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        self.started = time.monotonic()
        self.events: list[dict] = []
        self.refs: set[str] = set()
        self.messages: list[dict] = []
        self.model_calls: list[dict] = []
        self.tool_calls: list[dict] = []
        path.mkdir(parents=True, exist_ok=True)
        for name in ("events.v2", "messages", "model_calls", "tool_calls", "checks"):
            (path / (name + ".jsonl")).touch()

    def append(self, name: str, value: dict) -> None:
        with (self.path / (name + ".jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(encoded(value) + "\n")

    def event(self, kind: str, *, role: str | None = None, **data) -> dict:
        record = {
            "schema_version": "2.0", "run_id": self.run_id,
            "event_id": f"evt-{len(self.events) + 1:06d}",
            "sequence": len(self.events) + 1, "timestamp": now(),
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
            "kind": kind, "role": role, **data,
        }
        self.events.append(record)
        self.refs.add(record["event_id"])
        self.append("events.v2", record)
        return record

    def message(self, role: str, payload: dict, model_call_id: str, round_number: int) -> dict:
        refs = payload.get("evidence_refs", [])
        if any(ref not in self.refs for ref in refs):
            raise ValueError("存在未发生或其他运行的证据引用")
        message = {
            "schema_version": "2.0", "run_id": self.run_id,
            "message_id": f"msg-{len(self.messages) + 1:04d}",
            "parent_id": self.messages[-1]["message_id"] if self.messages else None,
            "sender": role, "recipient": payload.get("assignee") or "coordinator",
            "round": round_number, "timestamp": now(), "payload": payload,
            "model_call_id": model_call_id, "evidence_refs": refs,
        }
        self.messages.append(message)
        self.refs.add(message["message_id"])
        self.append("messages", message)
        self.event("role_output_submitted", role=role, round=round_number,
                   message_id=message["message_id"], model_call_id=model_call_id,
                   payload=payload, evidence_refs=refs)
        return message
