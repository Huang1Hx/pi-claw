"""有界 SSE 解析：聚合文本和分片参数，只产出完整有效的工具调用。"""

import json
from collections.abc import Iterator
from typing import Any

from core.schemas import ModelReply, ToolCall
from core.usage import TokenUsage
from providers.base import ProviderError
from providers.events import ProviderEvent


def parse_message(message: Any, usage: Any = None) -> ModelReply:
    """同步和流式响应共享最终消息校验。"""
    try:
        if not isinstance(message, dict):
            raise TypeError("message 不是对象")
        raw_calls = message.get("tool_calls") or []
        if raw_calls:
            calls = []
            for raw in raw_calls:
                function = raw["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                calls.append(ToolCall(raw["id"], function["name"], arguments))
            if len({c.id for c in calls}) != len(calls):
                raise ValueError("重复 tool_call id")
            reply = ModelReply.calls(calls)
        else:
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("缺少文本 content")
            reply = ModelReply.final(content)
        return ModelReply(
            final_text=reply.final_text,
            tool_calls=reply.tool_calls,
            usage=TokenUsage.from_dict(usage),
            reasoning_content=message.get("reasoning_content"),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderError("模型响应格式无效，工具参数必须为 JSON 对象") from exc


def iter_sse(response: Any, max_bytes: int) -> Iterator[dict[str, Any] | None]:
    """按 SSE 空行分帧，支持多 data 行；None 表示 [DONE]。"""
    used = 0
    data: list[str] = []
    while True:
        raw = response.readline(max_bytes - used + 1)
        if not raw:
            raise ProviderError("模型流意外中断：未收到 [DONE]")
        used += len(raw)
        if used > max_bytes:
            raise ProviderError(f"模型响应超过字节上限 {max_bytes}")
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise ProviderError("模型流不是有效 UTF-8") from exc
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload == "[DONE]":
                yield None
                return
            try:
                value = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ProviderError("模型 SSE 数据不是有效 JSON") from exc
            if not isinstance(value, dict) or "error" in value:
                raise ProviderError("模型 SSE 返回错误或无效对象")
            yield value


def stream_events(response: Any, max_bytes: int) -> Iterator[ProviderEvent]:
    """参数收齐且流正常结束后才生成 ModelReply，绝不执行半条工具。"""
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    usage = None
    for frame in iter_sse(response, max_bytes):
        if frame is None:
            break
        if frame.get("usage") is not None:
            usage = frame["usage"]
        choices = frame.get("choices", [])
        if not choices:
            continue
        try:
            choice = choices[0]
            if choice.get("index", 0) != 0:
                raise ValueError("只支持一个候选")
            delta = choice.get("delta") or {}
            if finish is not None and (
                delta.get("content") or delta.get("tool_calls") or delta.get("reasoning_content")
            ):
                raise ValueError("finish_reason 后仍出现内容")
            text = delta.get("content")
            thought = delta.get("reasoning_content")
            if thought is not None:
                if not isinstance(thought, str):
                    raise TypeError("reasoning_content 必须为文本")
                # 推理分片只在内存中聚合，用于同模型工具续接；不作为 text_delta 展示。
                reasoning.append(thought)
                yield ProviderEvent("reasoning_progress", {"chars": len(thought)})
            if text is not None:
                if not isinstance(text, str):
                    raise TypeError("content 必须为文本")
                content.append(text)
                if text:
                    yield ProviderEvent("text_delta", {"text": text, "provisional": True})
            # tool_call 参数可跨多个 SSE 分片；按 index 拼接，期间只发预览事件。
            for fragment in delta.get("tool_calls") or []:
                index = fragment["index"]
                if type(index) is not int or not 0 <= index < 16:
                    raise ValueError("tool_call index 超限")
                call = calls.setdefault(
                    index, {"id": "", "function": {"name": "", "arguments": ""}}
                )
                if fragment.get("id"):
                    if call["id"] and call["id"] != fragment["id"]:
                        raise ValueError("tool_call id 发生变化")
                    call["id"] = fragment["id"]
                function = fragment.get("function") or {}
                for key in ("name", "arguments"):
                    part = function.get(key)
                    if part is not None:
                        if not isinstance(part, str):
                            raise TypeError("工具分片必须为文本")
                        call["function"][key] += part
                yield ProviderEvent("tool_call_delta", {"index": index, "fragment": fragment})
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            raise ProviderError("模型流分片格式无效") from exc
    # 读到 [DONE] 还不够，必须有合法 finish_reason 并通过最终参数 JSON 校验。
    if finish not in {"stop", "tool_calls"}:
        raise ProviderError("模型流未正常完成（可能被截断或过滤），拒绝执行")
    if finish == "tool_calls" and not calls:
        raise ProviderError("模型流声明工具调用但没有工具数据")
    if finish == "stop" and calls:
        raise ProviderError("模型流包含工具数据，但 finish_reason 不是 tool_calls")
    reply = parse_message(
        {
            "content": "".join(content),
            "tool_calls": [calls[k] for k in sorted(calls)],
            "reasoning_content": "".join(reasoning) if reasoning else None,
        },
        usage,
    )
    yield ProviderEvent("message_complete", reply=reply)
