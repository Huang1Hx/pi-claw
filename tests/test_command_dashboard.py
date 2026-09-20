"""模型名称、精简状态、单一退出命令与跨会话用量的本地契约测试。"""

import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from prompt_toolkit.document import Document

from commands.model import model_options, resolve_model_selection
from config import ModelProfile, Settings, WorkflowSettings
from core.schemas import RunResult, RunStatus
from core.usage import TokenUsage, UsageTracker
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import OfflineCodingProvider


def make_repl(tmp_path, *, settings=None):
    """使用离线 Provider 和临时持久目录，命令测试不消耗模型 Token。"""
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    return CodingRepl(
        workspace=workspace,
        provider=OfflineCodingProvider(),
        output=io.StringIO(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        settings=settings
        or Settings(
            {
                "primary": ModelProfile(
                    "primary", "https://private.invalid", "deepseek-flash", "secret"
                ),
                "economy": ModelProfile(
                    "economy", "https://private.invalid", "qwen3.8-chat", "secret"
                ),
            }
        ),
    )


def counts(prompt=10, completion=5, *, unknown=False):
    """构造完整服务端响应账本，不把缺失 usage 伪造成零用量响应。"""
    tracker = UsageTracker()
    tracker.add(None if unknown else TokenUsage(prompt, completion, prompt + completion))
    return tracker.to_dict()


def test_exit_is_the_only_exit_menu_entry(tmp_path):
    """/quit 不再注册、补全或退出，不完整命令也不进入 Agent。"""
    repl = make_repl(tmp_path)
    menu = list(repl.terminal_input.completer.get_completions(Document("/"), None))
    assert "/exit" in {x.text for x in menu}
    assert "/quit" not in {x.text for x in menu}
    original = list(repl.messages)
    assert repl._handle_command("/quit")
    assert repl.messages == original
    assert not repl._handle_command("/exit")


def test_model_menu_shows_and_selects_actual_id(tmp_path):
    """实际模型 ID 展示/补全/选择一致，磁盘仍存 profile 以保留连接和参数。"""
    repl = make_repl(tmp_path)
    repl._handle_command("/model")
    text = repl.output.getvalue()
    assert "deepseek-flash → primary" in text and "qwen3.8-chat → economy" in text
    assert "secret" not in text and "private.invalid" not in text
    choices = list(repl.terminal_input.completer.get_completions(Document("/model deep"), None))
    assert [x.text for x in choices] == ["deepseek-flash"]
    old = repl.session.session_id
    repl._handle_command("/model deepseek-flash")
    assert repl.model == "primary" and repl.session.model == "primary"
    assert old != repl.session.session_id
    assert "deepseek-flash（固定模型" in repl.output.getvalue()
    assert repl.store.load(old, repl.workspace)


def test_ambiguous_model_id_requires_profile_and_keeps_session(tmp_path):
    """同一模型的不同 thinking/连接配置不能静默猜选，别名选择仍可用。"""
    repl = make_repl(tmp_path)
    profiles = {
        **repl.settings.profiles,
        "reviewer": replace(
            repl.settings.profiles["primary"], name="reviewer", thinking_mode="enabled"
        ),
    }
    repl.settings = replace(repl.settings, profiles=profiles)
    original = repl.session.session_id
    repl._handle_command("/model deepseek-flash")
    assert repl.session.session_id == original and "多个配置" in repl.output.getvalue()
    assert "deepseek-flash" not in model_options(repl.settings)
    repl._handle_command("/model reviewer")
    assert repl.model == "reviewer"


def test_auto_menu_explains_real_workflow_models(tmp_path):
    """自动路由显示角色和实际模型，不能宣称配置展示已完成可用性验证。"""
    repl = make_repl(tmp_path)
    repl.settings = replace(
        repl.settings,
        workflow=WorkflowSettings(
            enabled=True, planner="economy", coder="primary", reviewer="economy"
        ),
    )
    repl._handle_command("/model auto")
    repl._handle_command("/model")
    text = repl.output.getvalue()
    assert "自动路由" in text and "规划：qwen3.8-chat [economy]" in text
    assert "编码：deepseek-flash [primary]" in text and "不是可用性测试" in text


def test_no_configuration_has_no_fake_model_choices(tmp_path):
    """无配置时仍能选择离线模式，不伪造可用的真实模型。"""
    repl = make_repl(tmp_path, settings=Settings())
    assert model_options(repl.settings) == {"scripted": "离线演示，不调用真实模型"}
    repl._handle_command("/model")
    assert "未加载模型列表" in repl.output.getvalue()
    with pytest.raises(ValueError, match="加载模型"):
        resolve_model_selection("some-model", None)


def test_status_compact_and_verbose_have_distinct_purposes(tmp_path):
    """默认只展示五项状态，详细 JSON 保持调试字段以便解释原输出。"""
    repl = make_repl(tmp_path)
    repl._handle_command("/model deepseek-flash")
    repl.output = io.StringIO()
    repl._handle_command("/status")
    text = repl.output.getvalue()
    assert "模型：deepseek-flash" in text and "审批：按需确认" in text
    assert "尚未执行任务" in text and '"messages"' not in text and '"routing"' not in text
    repl.output = io.StringIO()
    repl._handle_command("/status --verbose")
    details = json.loads(repl.output.getvalue())
    assert details["messages"] == 1 and details["last_status"] is None
    assert details["provider"] == "routed" and details["model"] == "primary"


def test_status_after_failure_and_invalid_arguments(tmp_path):
    """失败和自动审批不被精简界面隐藏；无效参数报告错误而不执行任务。"""
    repl = make_repl(tmp_path)
    repl.auto_approve = True
    repl.last_result = RunResult(RunStatus.PROVIDER_ERROR, "run", 1)
    repl._handle_command("/status")
    assert "模型请求失败" in repl.output.getvalue() and "仍受策略限制" in repl.output.getvalue()
    repl._handle_command("/status --unknown")
    repl._handle_command("/usage --unknown")
    assert repl.output.getvalue().count("命令错误") == 2


def test_project_usage_is_persistent_isolated_and_idempotent(tmp_path):
    """同项目多会话累计，重复保存不加倍，其他工作区和其他 data_dir 不串入。"""
    repl = make_repl(tmp_path)
    store = repl.store
    current = repl.session.session_id
    for _ in range(2):
        store.record_usage(current, repl.workspace, "first", counts())
    other = store.create(repl.workspace, "system")
    store.record_usage(other.session_id, repl.workspace, "second", counts(20, 10))
    store.record_usage(other.session_id, repl.workspace, "unknown", counts(unknown=True))
    empty = store.create(repl.workspace, "system")
    elsewhere = tmp_path / "another-project"
    elsewhere.mkdir()
    foreign = store.create(elsewhere, "system")
    store.record_usage(foreign.session_id, elsewhere, "foreign", counts(1000, 1000))
    alternate = SessionStore(tmp_path / "different-data")
    hidden = alternate.create(repl.workspace, "system")
    alternate.record_usage(hidden.session_id, repl.workspace, "not-in-store", counts(500, 500))
    resumed = SessionStore(store.data_dir)
    report = resumed.workspace_usage(repl.workspace)
    assert report["totals"]["total_tokens"] == 45
    assert report["totals"]["requests_without_usage"] == 1
    assert report["session_count"] == 3 and report["recorded_sessions"] == 2
    assert report["unrecorded_sessions"] == 1 and report["unreadable_sessions"] == 0
    assert resumed.total_usage(empty.session_id, repl.workspace) == {}
    repl._handle_command("/usage")
    text = repl.output.getvalue()
    assert "当前会话｜已记录 Token：15" in text
    assert "当前项目累计｜已记录 Token：45" in text
    assert "usage 未知的响应：1" in text and "累计值可能不完整" in text


@pytest.mark.parametrize(
    "damage", ["json", "partial", "negative", "inconsistent", "symlink", "identity"]
)
def test_corrupt_usage_does_not_silently_become_zero(tmp_path, damage):
    """损坏/重定向/归属不符账本显式形成缺口，其他正常会话仍可统计。"""
    repl = make_repl(tmp_path)
    repl.store.record_usage(repl.session.session_id, repl.workspace, "good", counts())
    broken = repl.store.create(repl.workspace, "system")
    directory = repl.store._session_dir(broken.session_id, repl.workspace)
    path = directory / "usage.json"
    if damage == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"foreign": counts(100, 100)}))
        path.symlink_to(outside)
    elif damage == "json":
        path.write_text("{")
    elif damage == "partial":
        path.write_text(json.dumps({"broken": {"total_tokens": 200}}))
    elif damage == "negative":
        path.write_text(json.dumps({"broken": {**counts(), "total_tokens": -1}}))
    elif damage == "inconsistent":
        path.write_text(json.dumps({"broken": {**counts(), "requests": 9}}))
    else:
        state = json.loads((directory / "state.json").read_text())
        state["workspace"] = str(tmp_path)
        (directory / "state.json").write_text(json.dumps(state))
    report = repl.store.workspace_usage(repl.workspace)
    assert report["totals"]["total_tokens"] == 15
    assert report["unreadable_sessions"] == 1 and report["session_count"] == 2
    repl._handle_command("/usage")
    assert "不可读 1" in repl.output.getvalue()


def test_unreadable_current_session_still_shows_project_subtotal(tmp_path):
    """当前账本坏掉时不把整条 usage 命令吞掉，也不声称当前消耗为零。"""
    repl = make_repl(tmp_path)
    directory = repl.store._session_dir(repl.session.session_id, repl.workspace)
    (directory / "usage.json").write_text("{")
    repl._handle_command("/usage")
    text = repl.output.getvalue()
    assert "当前会话｜账本不可读，用量未知" in text and "当前项目累计" in text


def test_empty_project_usage_does_not_create_storage(tmp_path):
    """没有会话的项目只读查询返回空记录，不创建目录或迁移其他项目历史。"""
    store = SessionStore(tmp_path / "missing-store")
    report = store.workspace_usage(tmp_path)
    assert report["session_count"] == 0 and report["totals"] == {}
    assert not store.data_dir.exists()


def test_project_usage_rejects_symlink_group(tmp_path):
    """工作区分组目录不能通过符号链接指向另一个项目账本。"""
    store = SessionStore(tmp_path / "sessions")
    store.data_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    store._workspace_group(tmp_path).symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        store.workspace_usage(tmp_path)


def test_concurrent_same_run_is_only_counted_once(tmp_path):
    """写入仍按单会话文件锁幂等化；聚合只读且不再额外写总账。"""
    repl = make_repl(tmp_path)

    def save(_):
        """模拟独立保存者重复提交同一回合。"""
        store = SessionStore(repl.store.data_dir)
        store.record_usage(repl.session.session_id, repl.workspace, "same-run", counts())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(8)))
    assert repl.store.workspace_usage(repl.workspace)["totals"]["total_tokens"] == 15


def test_real_cli_can_select_model_and_show_usage_without_api_calls(tmp_path):
    """真实 CLI 管道执行查看/切换/退出；无效域名确保这条命令路径不依赖模型联网。"""
    config = tmp_path / "models.toml"
    config.write_text(
        '[models.primary]\nbase_url="https://unused.invalid/v1"\n'
        'model="deepseek-flash"\napi_key_env="PI_CLAW_TEST_KEY"\n'
        '[models.economy]\nbase_url="https://unused.invalid/v1"\n'
        'model="qwen3.8-chat"\napi_key_env="PI_CLAW_TEST_KEY"\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "interfaces",
            "chat",
            "--no-env",
            "--config",
            str(config),
            "--route",
            "primary",
            "--data-dir",
            str(tmp_path / "sessions"),
        ],
        input="/model\n/model deepseek-flash\n/status\n/usage\n/exit\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        env={**os.environ, "PI_CLAW_TEST_KEY": "offline-test-secret"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deepseek-flash → primary" in result.stdout
    assert "模型：deepseek-flash（固定模型" in result.stdout
    assert "当前项目累计｜已记录 Token：0" in result.stdout
    assert "恢复此会话" in result.stdout
    assert "route>" not in result.stdout and "offline-test-secret" not in result.stdout
