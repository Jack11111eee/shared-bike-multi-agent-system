"""DeepSeek Chat Completions tool-calling adapter, with bounded HTTP calls."""
from __future__ import annotations

import os
from urllib.parse import urlparse

from bike_agent.llm.provider import ModelError, ModelReply


class DeepSeekProvider:
    name = "deepseek"
    is_real = True

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or "https://api.deepseek.com").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ModelError("DEEPSEEK_BASE_URL 必须是无内嵌凭证的 HTTPS 地址")
        if not self._api_key:
            raise ModelError("缺少 DEEPSEEK_API_KEY，请在项目 .env.local 中配置")

    def _request(self, method: str, path: str, timeout: float, payload=None):
        try:
            import httpx
        except ImportError as exc:
            raise ModelError("缺少可选依赖：请安装 pip install -e '.[llm]'") from exc
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.request(
                    method, self.base_url + path,
                    headers={"Authorization": "Bearer " + self._api_key},
                    json=payload,
                )
            if response.status_code >= 400:
                # Avoid logging server bodies that may echo configuration or input.
                error = ModelError(f"DeepSeek HTTP {response.status_code}")
                error.retryable = response.status_code == 429 or response.status_code >= 500
                raise error
            return response.json(), response.headers.get("x-request-id")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            error = ModelError(f"DeepSeek 网络请求失败：{type(exc).__name__}")
            error.retryable = True
            raise error from None
        except ValueError as exc:
            raise ModelError("DeepSeek 返回不可解析的 JSON") from exc

    def list_models(self, timeout: float = 20.0) -> list[str]:
        data, _ = self._request("GET", "/models", timeout)
        return [row["id"] for row in data.get("data", []) if isinstance(row.get("id"), str)]

    def complete(self, *, messages: list[dict], tools: list[dict],
                 timeout: float) -> ModelReply:
        if not self.model:
            raise ModelError("缺少 DEEPSEEK_MODEL；请先运行 llm check 查看账号可用模型")
        payload = {
            "model": self.model, "messages": messages, "tools": tools,
            "max_tokens": 4096, "stream": False,
            "thinking": {"type": "disabled"},
        }
        data, header_id = self._request("POST", "/chat/completions", timeout, payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError("invalid role")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("DeepSeek 返回缺少有效 assistant message") from exc
        return ModelReply(
            message=message, model=data.get("model", self.model),
            request_id=header_id or data.get("id"), usage=data.get("usage"),
            finish_reason=choice.get("finish_reason"),
        )
