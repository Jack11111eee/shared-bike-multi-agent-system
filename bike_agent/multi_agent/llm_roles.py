"""Model-driven role invocation, bounded tool use and validated delivery."""
from __future__ import annotations

import json
import time
from pathlib import Path

import jsonschema

from bike_agent.llm.provider import ModelError, ModelLimitReached, remaining_timeout
from bike_agent.multi_agent.contracts_v2 import ROLE_OUTPUTS, role_tools
from bike_agent.multi_agent.journal import encoded, now, write_json


class ModelRoles:
    def __init__(self, provider, journal, *, deadline: float, max_calls=24,
                 request_timeout=60.0):
        self.provider, self.journal = provider, journal
        self.deadline, self.max_calls = deadline, max_calls
        self.request_timeout = request_timeout
        self.call_count = 0
        self.completed_roles: dict[str, list[str]] = {}
        self.prompts = {role: (Path(__file__).parent / "prompts" / (role + ".md")).read_text()
                        for role in ROLE_OUTPUTS}

    def request(self, role, messages, tools):
        for attempt in range(2):
            if self.call_count >= self.max_calls:
                raise ModelLimitReached("模型调用次数达到上限")
            timeout = remaining_timeout(self.deadline, self.request_timeout)
            self.call_count += 1
            call_id = f"model-{self.call_count:04d}"
            start = time.monotonic()
            record = {"model_call_id": call_id, "role": role, "attempt": attempt + 1,
                      "provider": self.provider.name, "requested_model": self.provider.model,
                      "real_provider": bool(self.provider.is_real), "started_at": now()}
            self.journal.event("model_request_started", role=role, model_call_id=call_id)
            try:
                reply = self.provider.complete(messages=messages, tools=tools, timeout=timeout)
                record.update(status="ok", returned_model=reply.model, usage=reply.usage,
                              provider_request_id=reply.request_id, finish_reason=reply.finish_reason)
                write_json(self.journal.path / "model_io" / (call_id + ".json"),
                           {"input": messages, "tools": tools, "response": reply.message})
            except Exception as exc:
                record.update(status="error", error=str(exc))
                self.journal.append("model_calls", {**record, "elapsed_ms": (time.monotonic()-start)*1000})
                self.journal.model_calls.append(record)
                self.journal.event("model_request_failed", role=role, model_call_id=call_id,
                                   error=str(exc))
                if attempt == 0 and getattr(exc, "retryable", False):
                    continue
                raise
            record["elapsed_ms"] = (time.monotonic() - start) * 1000
            self.journal.model_calls.append(record)
            self.journal.append("model_calls", record)
            self.journal.refs.add(call_id)
            self.journal.event("model_request_completed", role=role, model_call_id=call_id)
            return reply, call_id
        raise ModelError("模型调用未完成")

    def perform(self, role: str, context: dict, execute_tool, validate_output, round_number=0):
        self.journal.event("role_started", role=role, round=round_number,
                           context=context, summary=context.get("instruction", ""))
        messages = [{"role": "system", "content": self.prompts[role]},
                    {"role": "user", "content": encoded(context)}]
        definitions = role_tools(role)
        allowed = {row["function"]["name"]: row["function"]["parameters"] for row in definitions}
        corrections, business_calls = 0, 0
        for _ in range(5):
            reply, call_id = self.request(role, messages, definitions)
            assistant = {k: v for k, v in reply.message.items()
                         if k in {"role", "content", "tool_calls"}}
            messages.append(assistant)
            calls = assistant.get("tool_calls") or []
            if not calls:
                corrections += 1
                if corrections > 1:
                    raise ModelError(f"{role} 未提交合法结构化交付")
                messages.append({"role": "user", "content": "请使用工具并以 submit_role_output 提交，普通文本不是交付。"})
                continue
            submitted = None
            for call in calls:
                name = call.get("function", {}).get("name", "")
                try:
                    if name not in allowed:
                        raise ValueError(f"角色无权调用工具：{name}")
                    args = json.loads(call["function"]["arguments"])
                    jsonschema.validate(args, allowed[name])
                    if submitted is not None:
                        raise ModelError("角色交付后仍请求额外动作，整条响应被拒绝")
                    if name == "submit_role_output":
                        jsonschema.validate(args, ROLE_OUTPUTS[role])
                        validate_output(args)
                        submitted = args
                        output = {"accepted": True}
                    else:
                        business_calls += 1
                        if business_calls > 3:
                            raise ValueError("本角色最多3次业务工具调用，额度已用尽；不要再请求工具，请直接 submit_role_output 交付现有证据")
                        output = execute_tool(role, name, args)
                        output = {**output, "role_tool_calls_remaining": 3 - business_calls}
                except (ValueError, jsonschema.ValidationError) as exc:
                    corrections += 1
                    error = str(exc).split("\n")[0][:500]
                    self.journal.event("action_rejected", role=role, model_call_id=call_id,
                                       tool=name, error=error)
                    self.journal.append("checks", {"role": role, "ok": False, "error": error,
                                                    "model_call_id": call_id})
                    if corrections > 1:
                        raise ModelError(f"{role} 输出纠错失败：{error}") from exc
                    output = {"accepted": False, "error": error, "instruction": "修正后重新提交。"}
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": encoded(output)})
            if submitted is not None:
                message = self.journal.message(role, submitted, call_id, round_number)
                self.completed_roles.setdefault(role, []).append(call_id)
                return submitted, message
        raise ModelLimitReached(f"{role} 工具往返达到上限")
