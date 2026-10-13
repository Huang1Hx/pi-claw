"""内置 Skill 资源的合法性与只读约束。"""

from skills.models import MAX_PROMPT_CHARS, READ_ONLY_FORBIDDEN
from skills.registry import SkillRegistry, known_tools
from skills.selector import SkillSelector


def test_builtin_registry_has_two_representative_skills():
    reg = SkillRegistry.from_builtin()
    assert reg.ids() == ("python-test-repair", "repository-investigation")


def test_repository_investigation_is_read_only():
    for skill in SkillRegistry.from_builtin().list():
        assert skill.manifest.required_tools, "每个内置 Skill 都应声明所需工具"
        assert skill.prompt.strip()
        assert len(skill.prompt) <= skill.manifest.max_prompt_chars <= MAX_PROMPT_CHARS
        if skill.manifest.read_only:
            assert not (set(skill.manifest.required_tools) & READ_ONLY_FORBIDDEN)


def test_repository_investigation_declares_read_tools():
    skill = SkillRegistry.from_builtin().get("repository-investigation")
    assert skill is not None and skill.manifest.read_only
    assert set(skill.manifest.required_tools) <= known_tools()
    assert not (set(skill.manifest.required_tools) & READ_ONLY_FORBIDDEN)


def test_python_test_repair_declares_write_and_command_but_not_read_only():
    skill = SkillRegistry.from_builtin().get("python-test-repair")
    assert skill is not None and skill.manifest.read_only is False
    assert {"write_or_replace", "run_command"} <= set(skill.manifest.required_tools)


def test_builtin_manifests_load_with_known_tools():
    # known_tools 白名单来自真实工具注册表，确保内置 Skill 的工具名不会漂移
    for skill in SkillRegistry.from_builtin().list():
        assert set(skill.manifest.required_tools) <= known_tools()


def test_selector_matches_builtin_skills():
    selector = SkillSelector(SkillRegistry.from_builtin())
    assert selector.select("帮我定位这个函数的调用链").skill.id == "repository-investigation"
    assert selector.select("这段代码测试失败，请修复测试").skill.id == "python-test-repair"
