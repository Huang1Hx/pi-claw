"""确定性任务锚构建：会话内、有界、零模型调用。

锚只从**已在手**的结构化数据构建：当前明确目标、用户显式 pin、成功写入记录、验证器结果。
任意工具正文、模型推测、历史摘要都不会进入锚，也不会因进入锚而提升信任等级。
相同规范化输入与构建规则版本产生相同语义内容与 content_hash；updated_at 不参与确定性比较。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.active_context import ANCHOR_MAX_CHARS, TaskAnchor

_TASK_BUDGET = 800
_PINS_BUDGET = 1_200
_REASON_BUDGET = 300
_MAX_LISTED_FILES = 20


def _one_line(text: str, limit: int) -> str:
    """折叠空白并截断到上限；超长时用省略号显式标记。"""
    if not isinstance(text, str):
        raise TypeError("锚输入必须是字符串")
    collapsed = " ".join(text.split())
    if limit <= 0:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)] + "…"


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    """规范化字符串序列，拒绝非字符串元素。"""
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{name} 必须是字符串数组")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class AnchorInputs:
    """构建锚所需的、调用方已在手的数据；不含原始工具正文。"""

    task: str = ""
    pins: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    changed_files_incomplete: bool = False
    omitted_changed_files: int = 0
    verification_accepted: bool | None = None
    verification_reason: str = ""
    workspace_fingerprint: str = ""
    verified_workspace_fingerprint: str | None = None
    covered_through_seq: int = 0

    def __post_init__(self) -> None:
        _one_line(self.task, _TASK_BUDGET)
        object.__setattr__(self, "pins", _string_tuple(self.pins, "pins"))
        object.__setattr__(
            self, "changed_files", _string_tuple(self.changed_files, "changed_files")
        )
        if not isinstance(self.changed_files_incomplete, bool):
            raise TypeError("changed_files_incomplete 必须是布尔值")
        if type(self.omitted_changed_files) is not int or self.omitted_changed_files < 0:
            raise ValueError("omitted_changed_files 必须是非负整数")
        if self.verification_accepted is not None and not isinstance(
            self.verification_accepted, bool
        ):
            raise TypeError("verification_accepted 必须是布尔值或 None")
        if self.verified_workspace_fingerprint is not None and not isinstance(
            self.verified_workspace_fingerprint, str
        ):
            raise TypeError("verified_workspace_fingerprint 必须是字符串或 None")
        if type(self.covered_through_seq) is not int or self.covered_through_seq < 0:
            raise ValueError("covered_through_seq 必须是非负整数")


def build_anchor(inputs: AnchorInputs, *, updated_at: str = "") -> TaskAnchor | None:
    """由已在手数据构建有界锚；无任何内容时返回 None。

    约束（pins）是已接受的硬约束，无法容纳时显式报错，绝不静默截断。
    验证结果绑定工作区指纹：指纹在验证后被改动，则显式标注“已失效”。
    """

    if not isinstance(inputs, AnchorInputs):
        raise TypeError("inputs 必须是 AnchorInputs")

    task = _one_line(inputs.task, _TASK_BUDGET)
    # 约束是已接受的硬约束：只规范化空白，绝不截断；放不下就显式失败。
    pins = [" ".join(pin.split()) for pin in inputs.pins if pin.strip()]
    pin_chars = sum(len(pin) for pin in pins)
    if pin_chars > _PINS_BUDGET:
        raise ValueError(f"任务锚无法容纳全部约束（pins 合计 {pin_chars} 字符）")

    files = [name for name in inputs.changed_files if name.strip()]
    verification_stale = (
        inputs.verification_accepted is True
        and inputs.verified_workspace_fingerprint is not None
        and inputs.verified_workspace_fingerprint != inputs.workspace_fingerprint
    )

    if not task and not pins and not files and inputs.verification_accepted is None:
        return None

    lines: list[str] = []
    if task:
        lines.append(f"目标：{task}")
    if pins:
        lines.append("约束：" + "；".join(pins))
    if files or inputs.changed_files_incomplete:
        listed = files[:_MAX_LISTED_FILES]
        omitted = inputs.omitted_changed_files + max(0, len(files) - len(listed))
        segment = f"已改文件（{len(files)}）：{', '.join(listed)}" if listed else "已改文件（0）"
        if omitted:
            segment += f"（另有 {omitted} 个未列出）"
        if inputs.changed_files_incomplete:
            segment += "（写入记录不完整，可能遗漏）"
        lines.append(segment)
    if inputs.verification_accepted is not None:
        status = "通过" if inputs.verification_accepted else "未通过"
        reason = _one_line(inputs.verification_reason, _REASON_BUDGET)
        segment = f"验证：{status}" + (f"：{reason}" if reason else "")
        if verification_stale:
            segment += "（已失效：文件在上次验证后又被修改）"
        lines.append(segment)

    text = "\n".join(lines).strip()
    if len(text) > ANCHOR_MAX_CHARS:
        raise ValueError(f"任务锚超过上限 {ANCHOR_MAX_CHARS} 字符")

    sources: list[str] = []
    if task:
        sources.append("task")
    if pins:
        sources.append("pin")
    if files or inputs.changed_files_incomplete:
        sources.append("write-journal")
    if inputs.verification_accepted is not None:
        sources.append("verification")
    return TaskAnchor.build(
        text=text,
        covered_through_seq=inputs.covered_through_seq,
        sources=tuple(sources),
        updated_at=updated_at,
    )
