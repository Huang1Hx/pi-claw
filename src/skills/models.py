"""Skill 的声明式数据模型与严格校验；不包含任何可执行代码。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from skills.errors import ManifestError

# Skill ID：小写字母/数字，短横线分隔，2–64 字符，首尾为字母或数字。
SKILL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SEMVER_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")

# 只允许固定的提示文件名义，杜绝通过 prompt_file 指向任意文件。
PROMPT_FILE_ALLOWED = frozenset({"prompt.md"})

MAX_PROMPT_FILE_BYTES = 8192
MAX_PROMPT_CHARS = 8000
MAX_DESCRIPTION_CHARS = 300
MAX_TASK_TYPES = 16
MAX_TRIGGER_KEYWORDS = 32
MAX_REQUIRED_TOOLS = 8

# read_only 技能不得声明这些工具：写文件或可执行（可能破坏性）的命令。
READ_ONLY_FORBIDDEN = frozenset({"write_or_replace", "apply_patch", "run_command"})

MANIFEST_FIELDS = frozenset(
    {
        "id",
        "version",
        "description",
        "task_types",
        "trigger_keywords",
        "required_tools",
        "read_only",
        "max_prompt_chars",
        "prompt_file",
    }
)


class SelectionMethod(StrEnum):
    """Skill 选择方式，优先级由高到低。"""

    EXPLICIT = "explicit"
    TASK_TYPE = "task_type"
    KEYWORD = "keyword"
    NONE = "none"


@dataclass(frozen=True)
class SkillManifest:
    """严格校验后的 Skill 元数据。"""

    id: str
    version: str
    description: str
    task_types: tuple[str, ...]
    trigger_keywords: tuple[str, ...]
    required_tools: tuple[str, ...]
    read_only: bool
    max_prompt_chars: int
    prompt_file: str

    @classmethod
    def from_dict(cls, data: Any, *, known_tools: frozenset[str]) -> SkillManifest:
        """解析并严格校验 Manifest；任何未知字段或非法取值都拒绝。"""
        if not isinstance(data, dict):
            raise ManifestError("Manifest 必须是键值表")
        keys = set(data)
        unknown = keys - MANIFEST_FIELDS
        if unknown:
            raise ManifestError(f"Manifest 含未知字段：{sorted(unknown)}")
        missing = MANIFEST_FIELDS - keys
        if missing:
            raise ManifestError(f"Manifest 缺少字段：{sorted(missing)}")

        sid = _require_str(data, "id")
        if not SKILL_ID_RE.fullmatch(sid):
            raise ManifestError(f"非法 Skill ID：{sid!r}")

        version = _require_str(data, "version")
        if not SEMVER_RE.fullmatch(version):
            raise ManifestError(f"非法版本号：{version!r}")

        description = _require_str(data, "description")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ManifestError(f"description 超过 {MAX_DESCRIPTION_CHARS} 字符")

        task_types = _string_tuple(data, "task_types", max_items=MAX_TASK_TYPES, allow_empty=True)
        keywords = _string_tuple(
            data, "trigger_keywords", max_items=MAX_TRIGGER_KEYWORDS, allow_empty=True
        )
        required_tools = _string_tuple(
            data, "required_tools", max_items=MAX_REQUIRED_TOOLS, allow_empty=True
        )
        unknown_tools = [t for t in required_tools if t not in known_tools]
        if unknown_tools:
            raise ManifestError(f"required_tools 含未知工具：{unknown_tools}")

        read_only = data["read_only"]
        if not isinstance(read_only, bool):
            raise ManifestError("read_only 必须是布尔值")
        if read_only:
            forbidden = [t for t in required_tools if t in READ_ONLY_FORBIDDEN]
            if forbidden:
                raise ManifestError(f"只读 Skill 不得声明写/命令工具：{forbidden}")

        max_prompt_chars = data["max_prompt_chars"]
        if isinstance(max_prompt_chars, bool) or not isinstance(max_prompt_chars, int):
            raise ManifestError("max_prompt_chars 必须是整数")
        if max_prompt_chars <= 0 or max_prompt_chars > MAX_PROMPT_CHARS:
            raise ManifestError(f"max_prompt_chars 必须在 1..{MAX_PROMPT_CHARS} 之间")

        prompt_file = _require_str(data, "prompt_file")
        if prompt_file not in PROMPT_FILE_ALLOWED:
            raise ManifestError(f"prompt_file 不在白名单内：{prompt_file!r}")

        return cls(
            id=sid,
            version=version,
            description=description,
            task_types=task_types,
            trigger_keywords=keywords,
            required_tools=required_tools,
            read_only=read_only,
            max_prompt_chars=max_prompt_chars,
            prompt_file=prompt_file,
        )


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{key} 必须是非空字符串")
    return value


def _string_tuple(
    data: dict[str, Any], key: str, *, max_items: int, allow_empty: bool
) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list):
        raise ManifestError(f"{key} 必须是字符串数组")
    if not value and not allow_empty:
        raise ManifestError(f"{key} 不能为空")
    if len(value) > max_items:
        raise ManifestError(f"{key} 至多 {max_items} 项")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ManifestError(f"{key} 的每一项都必须是非空字符串")
        result.append(item)
    if len(set(result)) != len(result):
        raise ManifestError(f"{key} 不能包含重复项")
    return tuple(result)


@dataclass(frozen=True)
class Skill:
    """已加载的 Skill：元数据 + 受限提示文本 + 来源目录。"""

    manifest: SkillManifest
    prompt: str
    source: Path

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version


@dataclass(frozen=True)
class SkillSelection:
    """一次选择的结果；skill 为 None 时表示不激活。"""

    skill: Skill | None
    method: SelectionMethod
    reason: str
