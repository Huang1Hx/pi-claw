"""会话约束（/pin）与当前目标（/goal）命令。

命令只读写文件后端的活动快照（pins/goal/锚），不进入 LLM 工具、不执行 shell、不按路径加载任何文件。
写入使用修订号 CAS（``expected_revision``）：并发的原地更新只能有一个提交，冲突或存储失败显式上报，
不会显示为保存成功。
"""

from __future__ import annotations

from core.active_context import (
    GOAL_MAX_CHARS,
    MAX_PINS,
    PIN_MAX_CHARS,
    Pin,
)
from core.task_anchor import ANCHOR_PINS_BUDGET

_REMOVE_KEYS = {"-d", "rm", "remove", "delete"}


def _next_pin_id(pins: tuple[Pin, ...]) -> str:
    """生成确定性、会话内唯一的 pin_id；不复用已删除的编号。"""
    used = {pin.pin_id for pin in pins}
    index = 1
    while f"p{index}" in used:
        index += 1
    return f"p{index}"


def _list_pins(repl) -> bool:
    """列出当前约束；空列表时给出用法提示。"""
    pins = repl.load_context().pins
    if not pins:
        print("当前没有约束。用法：/pin <约束内容>；/pin -d <id>", file=repl.output)
        return True
    total = sum(len(pin.text) for pin in pins)
    for pin in pins:
        print(f"{pin.pin_id}｜seq={pin.seq}｜{pin.text}", file=repl.output)
    print(
        f"共 {len(pins)} 条约束（上限 {MAX_PINS} 条，总长 {total}/{ANCHOR_PINS_BUDGET} 字符）",
        file=repl.output,
    )
    return True


def _add_pin(repl, text: str) -> bool:
    """新增一条约束；超长、超量或放不进锚时拒绝写入。"""
    if len(text) > PIN_MAX_CHARS:
        raise ValueError(f"约束超长（{len(text)} > {PIN_MAX_CHARS} 字符）")
    context = repl.load_context()
    if len(context.pins) >= MAX_PINS:
        raise ValueError(f"约束数量已达上限 {MAX_PINS}")
    total = sum(len(pin.text) for pin in context.pins)
    if total + len(text) > ANCHOR_PINS_BUDGET:
        raise ValueError(f"约束总长超过任务锚上限 {ANCHOR_PINS_BUDGET} 字符")
    new_pin = Pin(pin_id=_next_pin_id(context.pins), text=text, seq=context.through_seq)
    updated = context.with_pins(context.pins + (new_pin,))
    repl.commit_context(
        updated,
        event="context_pinned",
        payload={"action": "add", "pin_id": new_pin.pin_id, "count": len(updated.pins)},
    )
    repl.sync_anchor()
    print(f"已添加约束 {new_pin.pin_id}：{new_pin.text}", file=repl.output)
    return True


def _remove_pin(repl, pin_id: str) -> bool:
    """按 id 删除约束；未知 id 显式报错，不静默忽略。"""
    if not pin_id:
        raise ValueError("用法：/pin -d <id>")
    context = repl.load_context()
    remaining = tuple(pin for pin in context.pins if pin.pin_id != pin_id)
    if len(remaining) == len(context.pins):
        raise ValueError(f"未知约束 id：{pin_id}")
    updated = context.with_pins(remaining)
    repl.commit_context(
        updated,
        event="context_pinned",
        payload={"action": "remove", "pin_id": pin_id, "count": len(remaining)},
    )
    repl.sync_anchor()
    print(f"已删除约束 {pin_id}。", file=repl.output)
    return True


def pin(repl, argument: str) -> bool:
    """查看、添加或删除约束。"""
    arg = argument.strip()
    if not arg:
        return _list_pins(repl)
    parts = arg.split(maxsplit=1)
    if parts[0] in _REMOVE_KEYS:
        return _remove_pin(repl, parts[1].strip() if len(parts) == 2 else "")
    return _add_pin(repl, arg)


def _print_pins_and_anchor(repl, context) -> None:
    """在展示目标时一并给出实际会注入模型的锚与约束构成。"""
    anchor = context.anchor
    if anchor is None:
        print("当前没有任务锚（未设置目标且无约束）。", file=repl.output)
    else:
        print(anchor.text, file=repl.output)


def goal(repl, argument: str) -> bool:
    """查看、更新或重置当前目标。"""
    arg = argument.strip()
    context = repl.load_context()
    if not arg:
        if not context.goal and context.anchor is None:
            print("未设置当前目标。用法：/goal <目标>；/goal reset", file=repl.output)
            return True
        if context.goal:
            print(f"当前目标：{context.goal}", file=repl.output)
        _print_pins_and_anchor(repl, context)
        return True
    if arg == "reset":
        updated = context.with_goal("").with_anchor(None)
        repl.commit_context(updated, event="anchor_updated", payload={"action": "reset"})
        repl.sync_anchor()
        print("已重置当前目标与任务锚。", file=repl.output)
        return True
    if len(arg) > GOAL_MAX_CHARS:
        raise ValueError(f"目标超长（{len(arg)} > {GOAL_MAX_CHARS} 字符）")
    updated = context.with_goal(arg)
    repl.commit_context(updated, event="anchor_updated", payload={"action": "set"})
    repl.sync_anchor()
    print(f"已设置当前目标：{arg}", file=repl.output)
    return True
