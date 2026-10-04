"""Chat Completions 同步/流式传输，有界读取和可观测的有限重试。"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from core.control import ExecutionControl
from core.schemas import ModelReply
from providers.base import ProviderError
from providers.events import ProviderEvent
from providers.streaming import parse_message, stream_events


class OpenAICompatibleProvider:
    """重试只覆盖尚未输出内容的暂时性请求失败，不重放工具。"""

    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 5_000_000,
        max_attempts: int = 3,
        retry_base_delay: float = 0.5,
        max_retry_delay: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        max_output_tokens: int | None = None,
        thinking_mode: str = "default",
        thinking_format: str = "thinking",
        profile_name: str | None = None,
        control: ExecutionControl | None = None,
    ) -> None:
        """保存成套连接参数并校验传输预算；sleep 可注入测试替身，构造阶段不发起请求。"""
        # None 才允许回退环境；显式空字符串必须保持为空，避免路由误用另一服务的 Key。
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL")
        if timeout <= 0 or max_response_bytes <= 0 or not 1 <= max_attempts <= 10:
            raise ValueError("Provider 超时、字节上限须为正，max_attempts 须在 1–10 之间")
        if retry_base_delay < 0 or max_retry_delay < 0:
            raise ValueError("重试等待时间不得为负")
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self.max_attempts, self.retry_base_delay = max_attempts, retry_base_delay
        self.max_retry_delay, self.sleep = max_retry_delay, sleep
        self._custom_sleep = sleep is not time.sleep
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens 必须是正整数")
        self.max_output_tokens = max_output_tokens
        if not isinstance(thinking_mode, str) or thinking_mode not in {
            "default",
            "enabled",
            "disabled",
        }:
            raise ValueError("thinking_mode 不受支持")
        if not isinstance(thinking_format, str) or thinking_format not in {
            "thinking",
            "chat_template_kwargs",
            "enable_thinking",
        }:
            raise ValueError("thinking_format 不受支持")
        self.thinking_mode, self.thinking_format = thinking_mode, thinking_format
        self.profile_name = profile_name
        self.control = control

    def set_control(self, control: ExecutionControl | None) -> None:
        """允许直接使用 Provider 的 Agent 也能共享运行截止时间。"""
        self.control = control

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] | None = None
    ) -> ModelReply:
        """兼容同步 API；HTTP 重试与流式传输共用实现。"""
        for event in self.events(messages, tools, stream=False):
            if event.reply is not None:
                return event.reply
        raise ProviderError("模型请求未返回完整消息")

    def stream(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] | None = None
    ) -> Iterator[ProviderEvent]:
        """逐片输出事件；消费到 message_complete 才能使用工具。"""
        yield from self.events(messages, tools, stream=True)

    def events(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> Iterator[ProviderEvent]:
        """401/400 不重试；部分流已输出后失败不重放。"""
        if not self.api_key:
            raise ProviderError("缺少 OPENAI_API_KEY（也可通过构造参数传入）")
        if not self.model:
            raise ProviderError("缺少 OPENAI_MODEL（也可通过构造参数传入）")
        payload: dict[str, Any] = {"model": self.model, "messages": self._wire_messages(messages)}
        if self.thinking_mode != "default":
            enabled = self.thinking_mode == "enabled"
            if self.thinking_format == "thinking":
                payload["thinking"] = {"type": self.thinking_mode}
            elif self.thinking_format == "chat_template_kwargs":
                payload["chat_template_kwargs"] = {"enable_thinking": enabled}
            else:
                payload["enable_thinking"] = enabled
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        if tools:
            payload.update(tools=list(tools), tool_choice="auto")
        if stream:
            payload.update(stream=True, stream_options={"include_usage": True})
        request = urllib.request.Request(
            self._chat_completions_url(),
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(1, self.max_attempts + 1):
            if self.control is not None:
                self.control.checkpoint()
            emitted = False
            retry_after = None
            try:
                request_timeout = (
                    self.control.limit_timeout(self.timeout)
                    if self.control is not None
                    else self.timeout
                )
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    if stream:
                        for event in stream_events(response, self.max_response_bytes):
                            if self.control is not None:
                                self.control.checkpoint()
                            emitted = True
                            yield event
                        if self.control is not None:
                            self.control.checkpoint()
                    else:
                        body = self._read_limited_body(response, "模型响应")
                        if self.control is not None:
                            self.control.checkpoint()
                        try:
                            data = json.loads(body)
                            choice = data["choices"][0]
                            if choice.get("finish_reason") in {"length", "content_filter"}:
                                raise ValueError("响应未完成")
                            reply = parse_message(choice["message"], data.get("usage"))
                        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
                            raise ProviderError(
                                "模型响应格式无效：缺少完整 choices[0].message"
                            ) from exc
                        yield ProviderEvent("message_complete", reply=reply)
                        if self.control is not None:
                            self.control.checkpoint()
                return
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    detail = self._read_limited_body(exc, "模型错误响应")[:1000]
                finally:
                    exc.close()
                if self.control is not None:
                    self.control.checkpoint()
                error = self._sanitize_error(f"模型服务返回 HTTP {status}：{detail}")
                retryable = status in self.RETRY_STATUS
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if self.control is not None:
                    self.control.checkpoint()
                error = self._sanitize_error(f"模型请求失败：{exc}")
                retryable = True
            # 一旦对外发出片段，自动重试会重复候选内容；此时必须失败并交给上层处理。
            if emitted or not retryable or attempt == self.max_attempts:
                raise ProviderError(error)
            delay = min(self.max_retry_delay, self.retry_base_delay * 2 ** (attempt - 1))
            try:
                if retry_after is not None:
                    delay = min(self.max_retry_delay, max(0.0, float(retry_after)))
            except (TypeError, ValueError):
                pass
            # 先通知再等待/重试，使路由预算层能在下一次 HTTP 请求前中止迭代。
            yield ProviderEvent(
                "llm_retry",
                {"attempt": attempt, "next_attempt": attempt + 1, "delay_seconds": delay},
            )
            if self.control is None or self._custom_sleep:
                self.sleep(delay)
                if self.control is not None:
                    self.control.checkpoint()
            else:
                delay = self.control.limit_timeout(delay) if delay > 0 else 0
                if delay > 0:
                    self.control.cancel_event.wait(delay)
                self.control.checkpoint()

    def reply(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] | None = None
    ) -> ModelReply:
        """兼容旧 Provider 调用名。"""
        return self.complete(messages, tools)

    def _chat_completions_url(self) -> str:
        """兼容 API 根地址和完整 endpoint。"""
        return (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else self.base_url + "/chat/completions"
        )

    def _wire_messages(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """移除宿主内部标记；跨 profile 不传递私有推理字段，不修改调用方历史。"""
        result = []
        for message in messages:
            item = {k: v for k, v in message.items() if k != "_pi_claw_profile"}
            source = message.get("_pi_claw_profile")
            if source is not None and source != self.profile_name:
                item.pop("reasoning_content", None)
            result.append(item)
        return result

    def _read_limited_body(self, response: Any, label: str) -> str:
        """即使缺少 Content-Length，也只读取预算加一个字节。"""
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                length = int(headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                length = 0
            if length > self.max_response_bytes:
                raise ProviderError(f"{label}超过字节上限 {self.max_response_bytes}")
        raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise ProviderError(f"{label}超过字节上限 {self.max_response_bytes}")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderError(f"{label}不是有效 UTF-8") from exc

    def _sanitize_error(self, message: str) -> str:
        """网络错误中移除已配置 key 和 Bearer 凭据。"""
        if self.api_key:
            message = message.replace(self.api_key, "[REDACTED]")
        return re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
