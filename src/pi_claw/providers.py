"""离线脚本 Provider 与可选 OpenAI-compatible Provider。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from .schemas import ModelReply, ToolCall


class ProviderError(RuntimeError):
    """Provider 的可读、已脱敏错误。"""


@runtime_checkable
class Provider(Protocol):
    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply: ...


class ScriptedProvider:
    """按顺序返回预设回复，完全离线且可重复。"""

    def __init__(self, replies: Iterable[ModelReply]) -> None:
        self._replies = tuple(replies)
        self._index = 0
        if not self._replies:
            raise ValueError("ScriptedProvider 至少需要一条回复")
        if not all(isinstance(reply, ModelReply) for reply in self._replies):
            raise TypeError("replies 中每项都必须是 ModelReply")

    @property
    def remaining(self) -> int:
        return len(self._replies) - self._index

    def reset(self) -> None:
        self._index = 0

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        del messages, tools
        if self._index >= len(self._replies):
            raise ProviderError("脚本回复已耗尽，Agent 尚未收到 final_text")
        reply = self._replies[self._index]
        self._index += 1
        return reply

    def reply(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        return self.complete(messages, tools)


class OpenAICompatibleProvider:
    """仅在 ``complete`` 被显式调用时发送 Chat Completions 请求。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL")
        self.timeout = timeout
        if timeout <= 0:
            raise ValueError("Provider timeout 必须大于 0")

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        if not self.api_key:
            raise ProviderError("缺少 OPENAI_API_KEY（也可通过构造参数传入）")
        if not self.model:
            raise ProviderError("缺少 OPENAI_MODEL（也可通过构造参数传入）")

        payload: dict[str, Any] = {"model": self.model, "messages": list(messages)}
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            self._chat_completions_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ProviderError(
                self._sanitize_error(f"模型服务返回 HTTP {exc.code}：{detail}")
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(self._sanitize_error(f"模型请求失败：{exc}")) from exc

        try:
            data = json.loads(body)
            message = data["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型响应格式无效：缺少 choices[0].message") from exc

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            if len(tool_calls) != 1:
                raise ProviderError("首版每轮只接受一个 tool_call")
            raw_call = tool_calls[0]
            try:
                function = raw_call["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise TypeError("arguments 不是对象")
                return ModelReply.call(
                    ToolCall(
                        id=raw_call["id"],
                        name=function["name"],
                        arguments=arguments,
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ProviderError("模型 tool_call 格式无效，arguments 必须是 JSON 对象") from exc

        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError("模型响应既没有 tool_call，也没有文本 content")
        return ModelReply.final(content)

    def reply(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        return self.complete(messages, tools)

    def _chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _sanitize_error(self, message: str) -> str:
        if self.api_key:
            message = message.replace(self.api_key, "[REDACTED]")
        message = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
        return message
