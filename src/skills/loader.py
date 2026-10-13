"""从内置目录安全加载声明式 Skill。

只读取固定文件名、限制大小、扫描秘密，并拒绝任何越出内置根目录的路径或符号链接。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from skills.errors import (
    DuplicateSkill,
    ManifestError,
    PathViolation,
    PromptTooLarge,
    SecretDetected,
)
from skills.models import (
    MAX_PROMPT_FILE_BYTES,
    Skill,
    SkillManifest,
)

MANIFEST_FILE = "skill.toml"

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*\S+"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
)


def _looks_like_secret(text: str) -> bool:
    """检测疑似密钥、凭据赋值或带用户名口令的 URL。"""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _within(child: Path, root: Path) -> bool:
    """判断 child 是否等于 root 或位于 root 之下。"""
    return child == root or root in child.parents


def load_skill(
    skill_dir: str | Path, *, builtin_root: str | Path, known_tools: frozenset[str]
) -> Skill:
    """加载并严格校验单个 Skill；任何不合规都抛错，不做部分接受。"""
    raw_dir = Path(skill_dir)
    if raw_dir.is_symlink():
        raise PathViolation("Skill 目录不得是符号链接")
    root = Path(builtin_root).expanduser().resolve(strict=False)
    resolved_dir = raw_dir.expanduser().resolve(strict=False)
    if not _within(resolved_dir, root):
        raise PathViolation("Skill 目录越出内置根目录")

    manifest_path = resolved_dir / MANIFEST_FILE
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ManifestError(f"缺少合法的 {MANIFEST_FILE}")
    try:
        parsed = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{MANIFEST_FILE} 无法解析：{exc}") from exc

    manifest = SkillManifest.from_dict(parsed, known_tools=known_tools)

    prompt_path = resolved_dir / manifest.prompt_file
    if not prompt_path.is_file() or prompt_path.is_symlink():
        raise PathViolation("提示文件缺失或为符号链接")
    resolved_prompt = prompt_path.resolve(strict=False)
    if not _within(resolved_prompt, resolved_dir):
        raise PathViolation("提示文件越出 Skill 目录")

    try:
        size = resolved_prompt.stat().st_size
    except OSError as exc:  # pragma: no cover - 竞态兜底
        raise ManifestError(f"提示文件不可读：{exc}") from exc
    if size > MAX_PROMPT_FILE_BYTES:
        raise PromptTooLarge(f"提示文件超过 {MAX_PROMPT_FILE_BYTES} 字节上限")

    text = resolved_prompt.read_text(encoding="utf-8", errors="strict")
    if not text.strip():
        raise ManifestError("提示文件不能为空")
    if len(text) > manifest.max_prompt_chars:
        raise PromptTooLarge(f"提示超过 Manifest 上限 {manifest.max_prompt_chars} 字符")
    if _looks_like_secret(text):
        raise SecretDetected("提示文件含疑似密钥、凭据或带凭据的 URL")

    return Skill(manifest=manifest, prompt=text, source=resolved_dir)


def load_builtin_skills(builtin_root: str | Path, *, known_tools: frozenset[str]) -> list[Skill]:
    """发现并加载内置根目录下的一层 Skill 子目录，按 ID 去重。"""
    root = Path(builtin_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        return []
    skills: list[Skill] = []
    seen: dict[str, Path] = {}
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        manifest_file = child / MANIFEST_FILE
        if not manifest_file.is_file():
            continue
        skill = load_skill(child, builtin_root=root, known_tools=known_tools)
        if skill.id in seen:
            raise DuplicateSkill(f"重复的 Skill ID：{skill.id}")
        seen[skill.id] = child
        skills.append(skill)
    return skills
