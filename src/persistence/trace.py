"""脱敏 JSONL 轨迹写入与纯只读回放。"""

from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
_SENSITIVE_KEYS = {
    "key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "authorization",
    "cookie",
    "set_cookie",
}
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|token|password|passwd|secret|"
    r"authorization|cookie))\s*([:=])\s*([^\s,;]+)"
)


def redact(value: Any, *, key: str | None = None) -> Any:
    """递归脱敏常见字段名与嵌入文本中的凭据模式。"""

    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = value.replace(sys.executable, "python")
        result = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", result)
        result = _OPENAI_KEY_PATTERN.sub(REDACTED, result)
        result = _ASSIGNMENT_PATTERN.sub(
            lambda match: f"{match.group(1)}{match.group(2)} {REDACTED}", result
        )
        return result
    return value


def _is_sensitive_key(key: str) -> bool:
    """规范化字段名后识别凭据字段；与文本模式脱敏配合，不能保证识别所有秘密。"""
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    suffixes = (
        "_api_key",
        "apikey",
        "_token",
        "_password",
        "_passwd",
        "_secret",
        "_authorization",
        "_cookie",
    )
    parts = set(normalized.split("_"))
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith(suffixes)
        or bool(parts & {"authorization", "cookie"})
    )


def _truncate_value(value: Any, *, string_limit: int, container_limit: int) -> Any:
    """递归限制字符串和容器，保留有效 JSON 并显式标记截断。"""

    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        return value[: max(0, string_limit - len(TRUNCATED))] + TRUNCATED
    if isinstance(value, dict):
        limited = {
            str(key): _truncate_value(
                item,
                string_limit=string_limit,
                container_limit=container_limit,
            )
            for key, item in value.items()
        }
        return _fit_container(limited, container_limit)
    if isinstance(value, (list, tuple)):
        limited = [
            _truncate_value(
                item,
                string_limit=string_limit,
                container_limit=container_limit,
            )
            for item in value
        ]
        return _fit_container(limited, container_limit)
    return value


def _fit_container(value: Any, limit: int) -> Any:
    """把过大的已脱敏对象转为带截断标记的 JSON 文本预览。"""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return value
    # preview 是已经脱敏的 JSON 文本，不会把原始凭据重新带回轨迹。
    preview_length = max(0, limit - len(TRUNCATED) - 40)
    return {
        "truncated": True,
        "preview": encoded[:preview_length] + TRUNCATED,
    }


class TraceWriter:
    """每次 record 立即追加一行，降低崩溃时丢失整段轨迹的风险。"""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        max_string_chars: int = 20_000,
        max_container_chars: int = 100_000,
        max_event_chars: int = 200_000,
    ) -> None:
        """创建轨迹父目录并设置逐行预算；实例线程锁仅保护本写入器的并发追加。"""
        if min(max_string_chars, max_container_chars, max_event_chars) <= 0:
            raise ValueError("轨迹字符上限必须全部大于 0")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex
        self.max_string_chars = max_string_chars
        self.max_container_chars = max_container_chars
        self.max_event_chars = max_event_chars
        self._lock = threading.Lock()

    def record(
        self,
        event: str,
        payload: Any,
        *,
        step: int = 0,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """写入带运行标识和步骤的脱敏事件。"""
        safe_payload = _truncate_value(
            redact(payload),
            string_limit=self.max_string_chars,
            container_limit=self.max_container_chars,
        )
        row: dict[str, Any] = {
            "run_id": _truncate_value(
                run_id or self.run_id,
                string_limit=self.max_string_chars,
                container_limit=self.max_container_chars,
            ),
            "step": step,
            "event": _truncate_value(
                event,
                string_limit=self.max_string_chars,
                container_limit=self.max_container_chars,
            ),
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "payload": safe_payload,
        }
        row, encoded = self._fit_event(row)
        # 本实例线程锁保护整行追加；不是 SessionStore 的跨进程锁，也不保证多个写入器互斥。
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
            stream.flush()
        return row

    def _fit_event(self, row: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """把最终 JSON 行限制到 max_event_chars，且始终保持可解析。"""

        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= self.max_event_chars:
            return row, encoded

        source = json.dumps(row["payload"], ensure_ascii=False, separators=(",", ":"))
        candidate = dict(row)
        candidate["payload"] = {"truncated": True, "preview": TRUNCATED}
        minimum = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(minimum) > self.max_event_chars:
            raise ValueError("max_event_chars 太小，无法容纳轨迹固定字段与截断标记")

        low, high = 0, len(source)
        best_row, best_encoded = candidate, minimum
        while low <= high:
            middle = (low + high) // 2
            attempt = dict(row)
            attempt["payload"] = {
                "truncated": True,
                "preview": source[:middle] + TRUNCATED,
            }
            attempt_encoded = json.dumps(
                attempt,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(attempt_encoded) <= self.max_event_chars:
                best_row, best_encoded = attempt, attempt_encoded
                low = middle + 1
            else:
                high = middle - 1
        return best_row, best_encoded

    def write_event(
        self, event: str, payload: Any, *, step: int = 0, run_id: str | None = None
    ) -> dict[str, Any]:
        """兼容轨迹事件写入入口。"""
        return self.record(event, payload, step=step, run_id=run_id)

    def __enter__(self) -> TraceWriter:
        """返回写入器自身以兼容 with；每次 record 独立打开和关闭文件。"""
        return self

    def __exit__(self, *_: object) -> None:
        """不吞掉调用方异常；逐次写入已关闭文件，无长期文件句柄需要释放。"""
        return None


TraceRecorder = TraceWriter


class TraceReplay:
    """只解析和格式化历史行；该类没有 ToolExecutor，因而不能重执行工具。"""

    def __init__(
        self,
        path: str | Path,
        *,
        max_events: int = 10_000,
        max_bytes: int = 20_000_000,
    ) -> None:
        """配置只读回放路径及大小上限；构造阶段不读取轨迹或实例化工具。"""
        if max_events <= 0 or max_bytes <= 0:
            raise ValueError("回放 max_events 与 max_bytes 必须大于 0")
        self.path = Path(path).expanduser()
        self.max_events = max_events
        self.max_bytes = max_bytes

    def read(self) -> list[dict[str, Any]]:
        """在大小限制内读取并校验 JSONL 轨迹。"""
        events: list[dict[str, Any]] = []
        total_bytes = 0
        line_number = 0
        with self.path.open("rb") as stream:
            while True:
                remaining = self.max_bytes - total_bytes
                raw_line = stream.readline(remaining + 1)
                if not raw_line:
                    break
                line_number += 1
                total_bytes += len(raw_line)
                if total_bytes > self.max_bytes:
                    raise ValueError(f"轨迹超过回放字节上限 {self.max_bytes}")
                if not raw_line.strip():
                    continue
                if len(events) >= self.max_events:
                    raise ValueError(f"轨迹超过回放事件上限 {self.max_events}")
                try:
                    line = raw_line.decode("utf-8")
                    value = json.loads(line)
                except UnicodeDecodeError as exc:
                    raise ValueError(f"轨迹第 {line_number} 行不是有效 UTF-8") from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(f"轨迹第 {line_number} 行不是有效 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"轨迹第 {line_number} 行必须是 JSON 对象")
                events.append(value)
        return events

    def replay(self) -> list[dict[str, Any]]:
        """返回历史事件供调用方检查，不产生工具副作用。"""
        return self.read()

    def format_lines(self, events: Iterable[dict[str, Any]] | None = None) -> list[str]:
        """将历史轨迹格式化为终端可读文本。"""
        source = self.read() if events is None else list(events)
        return [json.dumps(event, ensure_ascii=False, indent=2) for event in source]


def replay_trace(
    path: str | Path,
    *,
    max_events: int = 10_000,
    max_bytes: int = 20_000_000,
) -> list[dict[str, Any]]:
    """纯只读加载历史轨迹，不重新执行任何工具。"""
    return TraceReplay(path, max_events=max_events, max_bytes=max_bytes).read()
