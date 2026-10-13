"""SkillManifest 严格字段与取值校验。"""

import pytest

from skills.errors import ManifestError
from skills.models import (
    MAX_PROMPT_CHARS,
    SelectionMethod,
    SkillManifest,
)

KNOWN_TOOLS = frozenset(
    {"list_files", "search_text", "read_file", "write_or_replace", "apply_patch", "run_command"}
)


def base(**overrides):
    data = {
        "id": "repository-investigation",
        "version": "1.0.0",
        "description": "只读定位代码路径与调用链",
        "task_types": ["investigation"],
        "trigger_keywords": ["定位", "调用链"],
        "required_tools": ["read_file", "search_text"],
        "read_only": True,
        "max_prompt_chars": 2000,
        "prompt_file": "prompt.md",
    }
    data.update(overrides)
    return data


def test_valid_manifest_parses():
    m = SkillManifest.from_dict(base(), known_tools=KNOWN_TOOLS)
    assert m.id == "repository-investigation"
    assert m.version == "1.0.0"
    assert m.read_only is True
    assert m.required_tools == ("read_file", "search_text")


def test_unknown_field_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(extra="x"), known_tools=KNOWN_TOOLS)


def test_missing_field_rejected():
    data = base()
    del data["prompt_file"]
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(data, known_tools=KNOWN_TOOLS)


@pytest.mark.parametrize("bad", ["Bad-Id", "has space", "a..b", "../evil", "", "-lead", "UPPER"])
def test_bad_id_rejected(bad):
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(id=bad), known_tools=KNOWN_TOOLS)


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0.0", "", "1.0.x"])
def test_bad_version_rejected(bad):
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(version=bad), known_tools=KNOWN_TOOLS)


def test_unknown_tool_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(
            base(required_tools=["read_file", "os_shell"]), known_tools=KNOWN_TOOLS
        )


def test_duplicate_tool_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(
            base(required_tools=["read_file", "read_file"]), known_tools=KNOWN_TOOLS
        )


def test_read_only_with_write_tool_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(
            base(read_only=True, required_tools=["read_file", "write_or_replace"]),
            known_tools=KNOWN_TOOLS,
        )


def test_read_only_with_command_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(
            base(read_only=True, required_tools=["run_command"]), known_tools=KNOWN_TOOLS
        )


def test_non_readonly_write_allowed():
    m = SkillManifest.from_dict(
        base(read_only=False, required_tools=["read_file", "write_or_replace", "run_command"]),
        known_tools=KNOWN_TOOLS,
    )
    assert m.read_only is False


@pytest.mark.parametrize("bad", [0, -1, MAX_PROMPT_CHARS + 1, "2000", True])
def test_bad_max_prompt_chars_rejected(bad):
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(max_prompt_chars=bad), known_tools=KNOWN_TOOLS)


def test_bad_prompt_file_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(prompt_file="evil.py"), known_tools=KNOWN_TOOLS)


def test_wrong_type_for_lists_rejected():
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(task_types="investigation"), known_tools=KNOWN_TOOLS)
    with pytest.raises(ManifestError):
        SkillManifest.from_dict(base(trigger_keywords=[1, 2]), known_tools=KNOWN_TOOLS)


def test_selection_method_enum_values():
    assert {m.value for m in SelectionMethod} == {"explicit", "task_type", "keyword", "none"}
