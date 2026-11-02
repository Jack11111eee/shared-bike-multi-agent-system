"""The validated message store (F24).

A message is accepted only if it could have been produced by this run at this
point in time: the recipient exists, the id is unique, the parent and every
evidence reference already exist *and predate* the message, the round is still
open, and the payload matches its type's schema.  Everything else is rejected
and appended to ``rejections`` -- the audit trail keeps the attempt instead of
silently dropping it.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator

from bike_agent.multi_agent.contracts import (
    MESSAGE_SCHEMA,
    MESSAGE_TYPES,
    PAYLOAD_SCHEMAS,
    PROTOCOL_VERSION,
    ROLES,
)


class MessageRejected(ValueError):
    """An invalid message; ``record`` keeps what was attempted and why."""

    def __init__(self, reason: str, record: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.record = record

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class MessageStore:
    """One run's message log, with the protocol rules enforced on write."""

    def __init__(self, run_id: str, task_id: str):
        self.run_id = run_id
        self.task_id = task_id
        self.messages: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.current_round = 0
        self.closed_rounds: set[int] = set()
        self._order: dict[str, int] = {}
        self._tool_meta: dict[str, dict[str, Any]] = {}
        self._source = ""
        self._counter = 0
        self._message_counter = 0
        self._validator = Draft202012Validator(MESSAGE_SCHEMA)
        self._payload_validators = {
            name: Draft202012Validator(schema) for name, schema in PAYLOAD_SCHEMAS.items()
        }

    # -- ordering ----------------------------------------------------------
    def _next_order(self) -> int:
        self._counter += 1
        return self._counter

    def register_tool_call(
        self, tool_call_id: str, *, source: str | None = None, tool: str | None = None
    ) -> None:
        """Make a tool-call id referenceable by later messages."""
        if tool_call_id in self._order:
            raise MessageRejected(
                f"duplicate id {tool_call_id!r}",
                {"message_id": tool_call_id, "reason": "duplicate id"},
            )
        self._order[tool_call_id] = self._next_order()
        self._tool_meta[tool_call_id] = {"source": source, "tool": tool}

    # -- rounds ------------------------------------------------------------
    def open_round(self, round_number: int) -> None:
        if round_number in self.closed_rounds:
            raise ValueError(f"round {round_number} is already closed")
        self.current_round = max(self.current_round, int(round_number))

    def close_round(self, round_number: int) -> None:
        self.closed_rounds.add(int(round_number))

    # -- writing -----------------------------------------------------------
    def send(
        self,
        *,
        sender: str,
        recipient: str,
        type: str,
        round: int,
        payload: dict[str, Any],
        parent_id: str | None = None,
        evidence_refs: list[str] | tuple[str, ...] = (),
        status: str = "ok",
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and append one message; raise :class:`MessageRejected` otherwise."""
        attempted = {
            "sender": sender,
            "recipient": recipient,
            "type": type,
            "round": round,
            "payload": copy.deepcopy(payload),
            "parent_id": parent_id,
            "evidence_refs": list(evidence_refs),
            "status": status,
        }
        order = self._next_order()
        if message_id is None:
            self._message_counter += 1
            message_id = f"msg-{self._message_counter:04d}"
        message = {
            "schema_version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "message_id": message_id,
            "parent_id": parent_id,
            "sender": sender,
            "recipient": recipient,
            "type": type,
            "round": int(round),
            "task_id": self.task_id,
            "payload": copy.deepcopy(payload),
            "evidence_refs": list(evidence_refs),
            "status": status,
            "sequence": order,
            "timestamp": _now(),
        }
        self._validate(message, attempted)
        self.messages.append(message)
        self._order[message_id] = order
        return message

    def reject(self, reason: str, attempted: dict[str, Any]) -> None:
        """Record an invalid message without adding it to the log."""
        self.rejections.append(
            {"reason": reason, "attempted": copy.deepcopy(attempted), "at": _now()}
        )

    def _validate(self, message: dict[str, Any], attempted: dict[str, Any]) -> None:
        def fail(reason: str) -> None:
            self.reject(reason, attempted)
            raise MessageRejected(reason, attempted)

        if message["sender"] not in ROLES:
            fail(f"unknown sender {message['sender']!r}")
        if message["recipient"] not in ROLES:
            fail(f"unknown recipient {message['recipient']!r}")
        if message["type"] not in MESSAGE_TYPES:
            fail(f"unknown message type {message['type']!r}")
        if message["message_id"] in self._order:
            fail(f"duplicate message id {message['message_id']!r}")
        if not self.messages:
            if message["parent_id"] is not None:
                fail("the first message must have parent_id = null")
        else:
            if message["parent_id"] is None:
                fail("only the first message may have parent_id = null")
            if message["parent_id"] not in self._order:
                fail(f"dangling parent reference {message['parent_id']!r}")
            if self._order[message["parent_id"]] >= message["sequence"]:
                fail(f"parent {message['parent_id']!r} does not predate the message")
            # A reply must go back to whoever sent the message it answers.
            # ``task_assignment`` and ``revision_request`` start a new exchange,
            # and ``final`` closes the run, so they are exempt.
            if message["type"] not in ("task_assignment", "revision_request", "final"):
                parent = self.by_id(message["parent_id"])
                if parent is not None and parent["sender"] != message["recipient"]:
                    fail(
                        f"wrong recipient {message['recipient']!r}: parent "
                        f"{parent['message_id']!r} was sent by {parent['sender']!r}"
                    )
        if message["round"] < 0:
            fail("round must be >= 0")
        if message["round"] > self.current_round:
            fail(f"message claims future round {message['round']} (current {self.current_round})")
        if message["round"] in self.closed_rounds:
            fail(f"round {message['round']} is already closed")
        for reference in message["evidence_refs"]:
            if reference not in self._order:
                fail(f"dangling evidence reference {reference!r}")
            if self._order[reference] >= message["sequence"]:
                fail(f"evidence reference {reference!r} does not predate the message")
            meta = self._tool_meta.get(reference)
            if meta and meta.get("source") and meta["source"] != self._task_source():
                fail(
                    f"cross-source evidence {reference!r} "
                    f"({meta['source']} != {self._task_source()})"
                )
        errors = sorted(self._validator.iter_errors(message), key=str)
        if errors:
            fail("; ".join(error.message for error in errors))
        payload_errors = sorted(
            self._payload_validators[message["type"]].iter_errors(message["payload"]), key=str
        )
        if payload_errors:
            fail(
                f"invalid {message['type']} payload: "
                + "; ".join(error.message for error in payload_errors)
            )

    def _task_source(self) -> str:
        """The source every message in this run is about (from the task id)."""
        return getattr(self, "_source", "")

    def bind_source(self, source: str) -> None:
        self._source = source

    # -- reading -----------------------------------------------------------
    def by_id(self, message_id: str) -> dict[str, Any] | None:
        for message in self.messages:
            if message["message_id"] == message_id:
                return message
        return None

    def for_role(self, role: str) -> list[dict[str, Any]]:
        return [message for message in self.messages if message["recipient"] == role]

    def since(self, count: int) -> list[dict[str, Any]]:
        return self.messages[count:]

    def to_records(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.messages)
