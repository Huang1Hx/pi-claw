"""SkillLoader 的路径防护、长度上限与秘密扫描。"""

from pathlib import Path

import pytest

from skills.errors import (
    DuplicateSkill,
    ManifestError,
    PathViolation,
    PromptTooLarge,
    SecretDetected,
)
from skills.loader import load_builtin_skills, load_skill

KNOWN_TOOLS = frozenset(
    {"list_files", "search_text", "read_file", "write_or_replace", "apply_patch", "run_command"}
)

MANIFEST = """\
id = "{sid}"
version = "1.0.0"
description = "示例"
task_types = ["investigation"]
trigger_keywords = ["定位"]
required_tools = ["read_file"]
read_only = true
max_prompt_chars = 2000
prompt_file = "prompt.md"
"""


def make_skill(root: Path, sid: str = "demo-skill", prompt: str = "按步骤定位。") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    d = root / sid
    d.mkdir()
    (d / "skill.toml").write_text(MANIFEST.format(sid=sid), encoding="utf-8")
    (d / "prompt.md").write_text(prompt, encoding="utf-8")
    return d


def test_loads_valid_skill(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root)
    skill = load_skill(root / "demo-skill", builtin_root=root, known_tools=KNOWN_TOOLS)
    assert skill.manifest.id == "demo-skill"
    assert "定位" in skill.prompt


def test_skill_dir_outside_root_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root)
    outside = tmp_path / "outside"
    make_skill(outside, sid="evil")
    with pytest.raises(PathViolation):
        load_skill(outside / "evil", builtin_root=root, known_tools=KNOWN_TOOLS)


def test_traversal_dir_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root)
    with pytest.raises(PathViolation):
        load_skill(root / ".." / "builtin", builtin_root=root / "nope", known_tools=KNOWN_TOOLS)


def test_symlinked_prompt_rejected(tmp_path):
    root = tmp_path / "builtin"
    d = make_skill(root)
    (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")
    (d / "prompt.md").unlink()
    (d / "prompt.md").symlink_to(tmp_path / "secret.md")
    with pytest.raises(PathViolation):
        load_skill(d, builtin_root=root, known_tools=KNOWN_TOOLS)


def test_prompt_file_allowlist_enforced(tmp_path):
    root = tmp_path / "builtin"
    d = make_skill(root)
    (d / "skill.toml").write_text(
        MANIFEST.format(sid="demo-skill").replace('"prompt.md"', '"other.md"'),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_skill(d, builtin_root=root, known_tools=KNOWN_TOOLS)


def test_oversized_prompt_file_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root, prompt="x" * 9000)
    with pytest.raises(PromptTooLarge):
        load_skill(root / "demo-skill", builtin_root=root, known_tools=KNOWN_TOOLS)


def test_prompt_exceeding_manifest_cap_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root, prompt="y" * 2100)
    with pytest.raises(PromptTooLarge):
        load_skill(root / "demo-skill", builtin_root=root, known_tools=KNOWN_TOOLS)


def test_secret_in_prompt_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root, prompt="use sk-abcdefghijklmnopqrstuvwxyz to auth")
    with pytest.raises(SecretDetected):
        load_skill(root / "demo-skill", builtin_root=root, known_tools=KNOWN_TOOLS)


def test_blank_prompt_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root, prompt="   \n  ")
    with pytest.raises(ManifestError):
        load_skill(root / "demo-skill", builtin_root=root, known_tools=KNOWN_TOOLS)


def test_load_builtin_skills_discovers_all(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root, sid="skill-a")
    make_skill(root, sid="skill-b")
    skills = load_builtin_skills(root, known_tools=KNOWN_TOOLS)
    assert {s.manifest.id for s in skills} == {"skill-a", "skill-b"}


def test_duplicate_id_rejected(tmp_path):
    root = tmp_path / "builtin"
    make_skill(root, sid="a")
    # 第二个目录用相同 manifest.id
    d = root / "b"
    d.mkdir()
    (d / "skill.toml").write_text(MANIFEST.format(sid="a"), encoding="utf-8")
    (d / "prompt.md").write_text("正文", encoding="utf-8")
    with pytest.raises(DuplicateSkill):
        load_builtin_skills(root, known_tools=KNOWN_TOOLS)
