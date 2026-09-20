from __future__ import annotations

from pathlib import Path

from core.prompts import build_system_prompt, discover_project_context


def test_project_context_reads_only_named_root_entries(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (tmp_path / "README.md").write_text("project readme", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "README.md").write_text("nested secret", encoding="utf-8")

    context = discover_project_context(tmp_path)

    assert "### 来源：AGENTS.md" in context
    assert "agent rules" in context
    assert "### 来源：README.md" in context
    assert "project readme" in context
    assert "nested secret" not in context


def test_project_context_reads_uppercase_readme_once(tmp_path: Path) -> None:
    """大写扩展名在大小写敏感系统可读，在 macOS 不重复注入。"""
    (tmp_path / "README.MD").write_text("unique uppercase readme content", encoding="utf-8")

    context = discover_project_context(tmp_path)

    assert context.count("unique uppercase readme content") == 1
    assert context.count("### 来源：README.") == 1


def test_project_context_marks_truncation(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x" * 100, encoding="utf-8")
    context = discover_project_context(tmp_path, max_file_chars=20, max_total_chars=80)
    assert "[TRUNCATED]" in context
    assert len(context) <= 80


def test_system_prompt_contains_workflow_safety_uv_and_project_context(
    tmp_path: Path,
) -> None:
    prompt = build_system_prompt(tmp_path, "### 来源：README.md\nhello project")
    assert str(tmp_path.resolve()) in prompt
    assert "Inspect → Plan → Act → Verify → Report" in prompt
    assert "结构化工具" in prompt
    assert "不主动联网" in prompt
    assert "Python 项目优先使用 uv" in prompt
    assert "### 来源：README.md" in prompt
    assert "hello project" in prompt
