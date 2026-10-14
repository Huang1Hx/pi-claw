"""会话活动上下文的值对象：消息窗口、结构化 pins、任务锚和快照版本。

本模块只做确定性解析、校验与序列化，不访问模型、不读磁盘、不授予权限。
完整历史始终由 journal 保存；这里描述的是发给模型的有界窗口及其派生状态。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

SNAPSHOT_VERSION = 3
READ_VERSIONS = (2, 3)
ANCHOR_MAX_CHARS = 2_000
MAX_PINS = 32
PIN_MAX_CHARS = 500
PINS_MAX_CHARS = 4_000

# 锚的内容哈希绑定构建规则版本；规则改变时必须同步升级，避免旧哈希被误判为等价。
_ANCHOR_SCHEMA = "anchor-v1"


class ActiveContextError(ValueError):
    """活动上下文载荷不合法、版本不受支持或字段越界。"""


def _require_str(value: Any, name: str, *, allow_empty: bool = False) -> str:
    """校验必填字符串字段；拒绝非字符串和（默认）空串。"""
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ActiveContextError(f"{name} 必须是非空字符串")
    return value


def _require_non_negative_int(value: Any, name: str) -> int:
    """校验序号/修订号一类的非负整数，拒绝 bool 以免 True 被当作 1。"""
    if type(value) is not int or value < 0:
        raise ActiveContextError(f"{name} 必须是非负整数")
    return value


def _to_str_tuple(value: Any, name: str) -> tuple[str, ...]:
    """把来源列表规范化为去重稳定的字符串元组；排序保证哈希可重复。"""
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ActiveContextError(f"{name} 必须是字符串数组")
    return tuple(sorted(set(value)))


@dataclass(frozen=True, slots=True)
class Pin:
    """用户或宿主显式钉住的约束；文本保持低信任，不因被钉住而升级为指令。"""

    pin_id: str
    text: str
    seq: int = 0
    created_at: str = ""

    def __post_init__(self) -> None:
        _require_str(self.pin_id, "pin_id")
        _require_str(self.text, "pin.text")
        _require_non_negative_int(self.seq, "pin.seq")
        if not isinstance(self.created_at, str):
            raise ActiveContextError("pin.created_at 必须是字符串")
        if len(self.text) > PIN_MAX_CHARS:
            raise ActiveContextError(f"pin.text 长度超过 {PIN_MAX_CHARS}")

    def to_dict(self) -> dict[str, Any]:
        """序列化为快照载荷中的对象。"""
        return {
            "pin_id": self.pin_id,
            "text": self.text,
            "seq": self.seq,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Pin:
        """从严解析 pin；未知字段直接拒绝，避免静默接受未来语义。"""
        if not isinstance(value, dict):
            raise ActiveContextError("pin 必须是对象")
        unknown = set(value) - {"pin_id", "text", "seq", "created_at"}
        if unknown:
            raise ActiveContextError(f"pin 含未知字段：{sorted(unknown)}")
        return cls(
            pin_id=value.get("pin_id"),
            text=value.get("text"),
            seq=value.get("seq", 0),
            created_at=value.get("created_at", ""),
        )


@dataclass(frozen=True, slots=True)
class TaskAnchor:
    """会话内任务锚：当前目标、关键约束和已验证进展的确定性摘要。

    covered_through_seq 表示构建器检查到的日志位置，不代表最后一条证据的位置；
    content_hash 只由语义内容决定，updated_at 不参与，保证相同输入产生相同哈希。
    """

    text: str
    covered_through_seq: int = 0
    sources: tuple[str, ...] = ()
    updated_at: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ActiveContextError("anchor.text 必须是字符串")
        if len(self.text) > ANCHOR_MAX_CHARS:
            raise ActiveContextError(f"anchor.text 长度超过 {ANCHOR_MAX_CHARS}")
        _require_non_negative_int(self.covered_through_seq, "anchor.covered_through_seq")
        if not all(isinstance(item, str) for item in self.sources):
            raise ActiveContextError("anchor.sources 必须是字符串数组")
        if not isinstance(self.updated_at, str):
            raise ActiveContextError("anchor.updated_at 必须是字符串")
        expected = self.content_hash_for(self.text, self.covered_through_seq, self.sources)
        if self.content_hash and self.content_hash != expected:
            raise ActiveContextError("anchor.content_hash 与内容不一致")

    @staticmethod
    def content_hash_for(
        text: str,
        covered_through_seq: int,
        sources: tuple[str, ...] | list[str],
    ) -> str:
        """计算与构建规则版本绑定的确定性内容哈希；不包含时间字段。"""
        normalized = sorted(set(sources))
        payload = [_ANCHOR_SCHEMA, text, covered_through_seq, normalized]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def build(
        cls,
        *,
        text: str,
        covered_through_seq: int = 0,
        sources: tuple[str, ...] | list[str] = (),
        updated_at: str = "",
    ) -> TaskAnchor:
        """由语义内容构造锚并自动计算 content_hash。"""
        normalized = tuple(sorted(set(sources)))
        return cls(
            text=text,
            covered_through_seq=covered_through_seq,
            sources=normalized,
            updated_at=updated_at,
            content_hash=cls.content_hash_for(text, covered_through_seq, normalized),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为快照载荷中的对象。"""
        return {
            "text": self.text,
            "covered_through_seq": self.covered_through_seq,
            "sources": list(self.sources),
            "updated_at": self.updated_at,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TaskAnchor:
        """从严解析锚；重新计算 content_hash 并与载荷比对，防止被篡改。"""
        if not isinstance(value, dict):
            raise ActiveContextError("anchor 必须是对象")
        unknown = set(value) - {
            "text",
            "covered_through_seq",
            "sources",
            "updated_at",
            "content_hash",
        }
        if unknown:
            raise ActiveContextError(f"anchor 含未知字段：{sorted(unknown)}")
        sources = _to_str_tuple(value.get("sources", ()), "anchor.sources")
        stored_hash = value.get("content_hash", "")
        if not isinstance(stored_hash, str):
            raise ActiveContextError("anchor.content_hash 必须是字符串")
        anchor = cls(
            text=value.get("text", ""),
            covered_through_seq=value.get("covered_through_seq", 0),
            sources=sources,
            updated_at=value.get("updated_at", ""),
            content_hash=stored_hash,
        )
        return anchor


@dataclass(frozen=True, slots=True)
class ActiveContext:
    """一份活动快照：普通消息窗口 + 水位线 + 修订号 + pins + 任务锚。

    messages 只存普通活动消息；pins 和 anchor 结构化保存，不混进 messages，
    避免同一权威数据出现两份、也避免被压缩摘要当成历史文本吞掉。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    through_seq: int = 0
    # 修订号目前只是随写入递增的计数，用于区分“水位线不变的原地编辑”；
    # 基于它的并发 CAS 校验在 M2/M4 实现，M0 不声称已提供该保护。
    snapshot_revision: int = 0
    pins: tuple[Pin, ...] = ()
    anchor: TaskAnchor | None = None
    version: int = SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.messages, list) or not all(
            isinstance(item, dict) for item in self.messages
        ):
            raise ActiveContextError("messages 必须是消息对象数组")
        _require_non_negative_int(self.through_seq, "through_seq")
        _require_non_negative_int(self.snapshot_revision, "snapshot_revision")
        if self.version != SNAPSHOT_VERSION:
            raise ActiveContextError(f"内存中的上下文版本必须是 {SNAPSHOT_VERSION}")
        if not isinstance(self.pins, tuple) or not all(isinstance(pin, Pin) for pin in self.pins):
            raise ActiveContextError("pins 必须是 Pin 元组")
        if self.anchor is not None and not isinstance(self.anchor, TaskAnchor):
            raise ActiveContextError("anchor 必须是 TaskAnchor 或 None")
        if len(self.pins) > MAX_PINS:
            raise ActiveContextError(f"pins 数量超过 {MAX_PINS}")
        pin_ids = [pin.pin_id for pin in self.pins]
        if len(set(pin_ids)) != len(pin_ids):
            raise ActiveContextError("pins 含重复 pin_id")
        if sum(len(pin.text) for pin in self.pins) > PINS_MAX_CHARS:
            raise ActiveContextError(f"pins 文本总长度超过 {PINS_MAX_CHARS}")

    @classmethod
    def empty(cls) -> ActiveContext:
        """空上下文，用于会话尚无 checkpoint 时。"""
        return cls()

    @classmethod
    def from_payload(cls, value: Any) -> ActiveContext:
        """解析 v2/v3 快照载荷；v2 升级为当前格式且 pins/anchor 为空。

        v1（list 旧格式）没有水位线，无法在此证明吸收了哪段日志，由持久化层回退到 journal。
        """
        if not isinstance(value, dict):
            raise ActiveContextError("活动上下文必须是对象")
        version = value.get("version")
        if type(version) is not int or version not in READ_VERSIONS:
            raise ActiveContextError("活动上下文版本不受支持")
        # messages 与 through_seq 是必填字段：缺失即视为畸形，不能默认成空数据。
        if "messages" not in value:
            raise ActiveContextError("活动上下文缺少 messages 字段")
        messages = value["messages"]
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise ActiveContextError("活动上下文必须包含消息对象数组")
        if "through_seq" not in value:
            raise ActiveContextError("活动上下文缺少 through_seq 字段")
        # snapshot_revision/pins/anchor 是可选兼容字段，缺失按默认值升级。
        pins_value = value.get("pins", [])
        if not isinstance(pins_value, list):
            raise ActiveContextError("pins 必须是数组")
        anchor_value = value.get("anchor")
        return cls(
            messages=list(messages),
            through_seq=value["through_seq"],
            snapshot_revision=value.get("snapshot_revision", 0),
            pins=tuple(Pin.from_dict(item) for item in pins_value),
            anchor=None if anchor_value is None else TaskAnchor.from_dict(anchor_value),
        )

    def to_payload(self) -> dict[str, Any]:
        """序列化为当前版本快照；省略默认值字段，使无 pins/anchor 的载荷与 v2 体积等价。

        消息未做脱敏，脱敏由持久化层负责；读取端对缺失字段按默认值处理。
        """
        payload: dict[str, Any] = {
            "version": SNAPSHOT_VERSION,
            "through_seq": self.through_seq,
            "messages": self.messages,
        }
        if self.snapshot_revision:
            payload["snapshot_revision"] = self.snapshot_revision
        if self.pins:
            payload["pins"] = [pin.to_dict() for pin in self.pins]
        if self.anchor is not None:
            payload["anchor"] = self.anchor.to_dict()
        return payload

    def with_messages(self, messages: list[dict[str, Any]], through_seq: int) -> ActiveContext:
        """替换消息窗口与水位线，保留 pins/anchor。

        只有在水位线不变、消息内容确实变化（如 /compact）时才推进 snapshot_revision：
        推进水位线的追加已由 expected_seq 保护，无需再增加一个版本维度，也避免
        无 pins/anchor 的默认快照因修订号字段变大而破坏既有字节预算。
        """
        new_messages = list(messages)
        in_place_edit = through_seq == self.through_seq and new_messages != self.messages
        return ActiveContext(
            messages=new_messages,
            through_seq=through_seq,
            snapshot_revision=self.snapshot_revision + 1
            if in_place_edit
            else self.snapshot_revision,
            pins=self.pins,
            anchor=self.anchor,
        )

    def with_pins(self, pins: tuple[Pin, ...]) -> ActiveContext:
        """替换 pins，保留其他字段并推进修订号。"""
        return ActiveContext(
            messages=self.messages,
            through_seq=self.through_seq,
            snapshot_revision=self.snapshot_revision + 1,
            pins=pins,
            anchor=self.anchor,
        )

    def with_anchor(self, anchor: TaskAnchor | None) -> ActiveContext:
        """替换任务锚，保留其他字段并推进修订号。"""
        return ActiveContext(
            messages=self.messages,
            through_seq=self.through_seq,
            snapshot_revision=self.snapshot_revision + 1,
            pins=self.pins,
            anchor=anchor,
        )
