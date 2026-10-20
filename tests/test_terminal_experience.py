"""工作区默认边界、真实终端斜杠菜单、回复去重与原始会话保真。"""

import io
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from prompt_toolkit.document import Document

from commands import create_commands
from commands.model import model_options
from config import ModelProfile, Settings
from config.workspace import resolve_workspace
from core.events import AgentEvent
from core.schemas import ModelReply, RunResult, RunStatus, ToolCall
from interfaces.cli import main
from interfaces.rendering import ReplyDisplay
from interfaces.repl import CodingRepl
from interfaces.terminal_input import SlashCompleter, TerminalInput
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import ScriptedProvider
from providers.events import ProviderEvent


def event(event_type, **data):
    """构建无敏感数据的界面事件。"""
    return AgentEvent(event_type, "test-run", 1, 1, data)


def test_default_workspace_is_created_without_moving_launcher(tmp_path, monkeypatch):
    """默认隔离目录不能改变 .env/相对配置解析的启动位置。"""
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace(None) == tmp_path / "projects"
    assert Path.cwd() == tmp_path
    assert list((tmp_path / "projects").iterdir()) == []
    assert resolve_workspace(tmp_path) == tmp_path


@pytest.mark.parametrize("kind", ["file", "symlink", "broken_symlink"])
def test_default_workspace_rejects_unsafe_targets(tmp_path, kind):
    """不把自动创建目录偷偷重定向到宿主源码或别的目录。"""
    target = tmp_path / "projects"
    if kind == "file":
        target.write_text("keep me")
    else:
        target.symlink_to(tmp_path if kind == "symlink" else tmp_path / "absent")
    with pytest.raises((ValueError, OSError)):
        resolve_workspace(None, cwd=tmp_path)
    assert target.is_symlink() or target.read_text() == "keep me"


def test_sessions_listing_has_no_default_workspace_creation(tmp_path, monkeypatch):
    """只读查看命令不能创建示例目录或搬走旧工作区的会话。"""
    monkeypatch.chdir(tmp_path)
    assert main(["sessions", "--data-dir", str(tmp_path / "sessions")]) == 0
    assert not (tmp_path / "projects").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_saves_actual_code_inside_selected_workspace(tmp_path, monkeypatch, capsys, explicit):
    """真实 CLI/工具/验收写文件；不把示例放到启动根目录或旧会话中。

    默认完成验收要求写入后出现测试绿灯，因此脚本按真实流程补写并运行一个单元测试；
    只跑 compileall 的旧宽松行为已不再满足完成验收。
    """
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "selected" if explicit else tmp_path / "projects"
    if explicit:
        target.mkdir()
    script = ScriptedProvider(
        [
            ModelReply.call(
                ToolCall(
                    "write",
                    "write_or_replace",
                    {
                        "path": "hello.py",
                        "old": "",
                        "new": 'print("Hello, World!")\n',
                    },
                )
            ),
            ModelReply.call(
                ToolCall(
                    "test",
                    "write_or_replace",
                    {
                        "path": "test_hello.py",
                        "old": "",
                        "new": (
                            "import unittest\n\nimport hello\n\n\n"
                            "class HelloTest(unittest.TestCase):\n"
                            "    def test_import(self):\n        self.assertTrue(True)\n"
                        ),
                    },
                )
            ),
            ModelReply.call(
                ToolCall(
                    "verify",
                    "run_command",
                    {
                        "argv": [
                            "uv",
                            "run",
                            "python",
                            "-m",
                            "unittest",
                            "-q",
                            "test_hello.py",
                        ],
                    },
                )
            ),
            ModelReply.final("示例已保存并通过单元测试"),
        ]
    )
    monkeypatch.setattr("interfaces.cli.OfflineCodingProvider", lambda: script)
    args = [
        "chat",
        "写一个 hello world，用 python",
        "--no-env",
        "--print",
        "--yes",
        "--data-dir",
        str(tmp_path / "sessions"),
    ]
    if explicit:
        args += ["--workspace", str(target)]
    assert main(args) == 0
    assert (target / "hello.py").read_text() == 'print("Hello, World!")\n'
    assert not (tmp_path / "hello.py").exists()
    store = SessionStore(tmp_path / "sessions")
    assert len(store.list_sessions(target)) == 1 and store.list_sessions(tmp_path) == []
    assert f"workspace: {target}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/", {"/exit", "/status", "/help", "/model"}),
        ("/sta", {"/status"}),
        ("/qu", set()),
        ("/model ", {"auto", "scripted", "economy", "primary", "small", "large"}),
        ("/model pr", {"primary"}),
        ("你好", set()),
        ("/title 我的", set()),
        ("请解释 /status", set()),
        ("/model primary extra", set()),
    ],
)
def test_slash_completer_uses_registry_and_profiles(text, expected):
    """菜单无需模型 API，不把普通文本当命令，不暴露模型 Key。"""
    settings = Settings(
        {
            "economy": ModelProfile("economy", "https://example.invalid", "small", "secret"),
            "primary": ModelProfile("primary", "https://example.invalid", "large", "secret"),
        }
    )
    completer = SlashCompleter(create_commands(), lambda: model_options(settings))
    values = list(completer.get_completions(Document(text), None))
    actual = {v.text for v in values}
    assert actual >= expected if text == "/" else actual == expected
    if text == "/sta":
        assert values[0].start_position == -4


def test_plain_input_fallback_and_bare_slash_are_local(tmp_path, monkeypatch):
    """管道输入支持 / 回车和命令提示，错误前缀不发送给模型。"""
    monkeypatch.setattr("builtins.input", lambda prompt: "/")
    terminal = TerminalInput(create_commands(), lambda: {}, io.StringIO())
    assert terminal.read("test> ") == "/" and terminal.session is None
    output = io.StringIO()
    repl = CodingRepl(
        workspace=tmp_path,
        provider=ScriptedProvider([ModelReply.final("unused")]),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        output=output,
    )
    before = list(repl.messages)
    assert repl._handle_command("/")
    assert repl._handle_command("/sta")
    assert "/status" in output.getvalue() and "可补全" in output.getvalue()
    assert repl.messages == before


def test_stream_body_appears_once_and_journal_keeps_final(tmp_path):
    """流式显示不二次打印完整答复，持久化仍保存完整 assistant 消息。"""

    class Stream:
        """确定性文本分片，可重复运行以检验每回合显示状态隔离。"""

        def events(self, messages, tools, *, stream):
            """真实 Agent 消费和校验事件。"""
            yield ProviderEvent("text_delta", {"text": "Hello, "})
            yield ProviderEvent("text_delta", {"text": "World!"})
            yield ProviderEvent("message_complete", reply=ModelReply.final("Hello, World!"))

    output = io.StringIO()
    repl = CodingRepl(
        workspace=tmp_path,
        provider=Stream(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        output=output,
    )
    for _ in range(2):
        assert repl.run_turn("只展示问候语") == 0
    assert output.getvalue().count("Hello, World!") == 2
    assert "assistant>" not in output.getvalue()
    saved = repl.store.load_messages(repl.session.session_id, tmp_path)
    assert sum(m.get("content") == "Hello, World!" for m in saved) == 2


def test_workflow_only_appends_review_conclusion():
    """编码正文已流式展示，独立审查产生的新结论仍保留而不重印代码说明。"""
    output = io.StringIO()
    display = ReplyDisplay(output)
    display.on_event(event("model_routed", purpose="coding"))
    display.on_event(event("text_delta", text="已完成编码"))
    display.on_event(
        event("workflow_artifact", kind="coding_candidate", data={"summary": "已完成编码"})
    )
    display.on_event(event("model_routed", purpose="reviewing"))
    display.on_event(event("model_reply", tool_call={"name": "read_file"}))
    display.on_event(event("tool_result", ok=True))
    display.on_event(event("model_routed", purpose="reviewing"))
    final = "已完成编码\n\n审查结论：通过"
    display.on_event(event("model_reply", final_text=final))
    display.finish(RunResult(RunStatus.COMPLETED, "run", 1, final_text=final))
    assert output.getvalue().count("已完成编码") == 1
    assert output.getvalue().count("审查结论：通过") == 1


def test_tool_whitespace_does_not_show_an_empty_candidate():
    """真实模型工具前的空白不产生标签；后续正常分片仍保留缩进并只展示一次。"""
    output = io.StringIO()
    display = ReplyDisplay(output)
    display.on_event(event("text_delta", text="\n\n"))
    display.on_event(event("model_reply", tool_call={"name": "list_files"}))
    assert output.getvalue() == ""
    display.on_event(event("text_delta", text="    "))
    display.on_event(event("text_delta", text="Hello"))
    display.on_event(event("model_reply", final_text="    Hello"))
    display.finish(RunResult(RunStatus.COMPLETED, "run", 1, final_text="    Hello"))
    assert "candidate(stream)>     Hello" in output.getvalue()
    assert output.getvalue().count("candidate(stream)>") == 1
    assert output.getvalue().count("Hello") == 1


def test_stream_retry_resets_candidate_deduplication():
    """前一次不完整候选作废后，新候选的正式回复只显示一次。"""
    output = io.StringIO()
    display = ReplyDisplay(output)
    display.on_event(event("text_delta", text="partial"))
    display.on_event(event("llm_retry", partial_output_invalidated=True))
    display.on_event(event("text_delta", text="complete"))
    display.on_event(event("model_reply", final_text="complete"))
    display.finish(RunResult(RunStatus.COMPLETED, "run", 1, final_text="complete"))
    assert output.getvalue().count("partial") == 1
    assert output.getvalue().count("complete") == 1
    assert "assistant>" not in output.getvalue()


def test_review_retry_keeps_previously_shown_coding_candidate():
    """只重试审查请求时，已展示的编码候选仍只显示一次。"""
    output = io.StringIO()
    display = ReplyDisplay(output)
    display.on_event(event("model_routed", purpose="coding"))
    display.on_event(event("text_delta", text="已完成编码"))
    display.on_event(
        event("workflow_artifact", kind="coding_candidate", data={"summary": "已完成编码"})
    )
    display.on_event(event("model_routed", purpose="reviewing"))
    display.on_event(event("llm_retry", partial_output_invalidated=True))
    final = "已完成编码\n\n审查结论：通过"
    display.on_event(event("model_reply", final_text=final))
    display.finish(RunResult(RunStatus.COMPLETED, "run", 1, final_text=final))
    assert output.getvalue().count("已完成编码") == 1
    assert output.getvalue().count("审查结论：通过") == 1


@pytest.mark.parametrize(
    "streamed,final,status",
    [
        ("old", "corrected", RunStatus.COMPLETED),
        ("partial", "partial + rest", RunStatus.COMPLETED),
        ("", "同步结果", RunStatus.COMPLETED),
        ("not verified", "not verified", RunStatus.UNVERIFIED),
    ],
)
def test_display_keeps_corrections_nonstream_and_failure(streamed, final, status):
    """只有完全相同才去重，不隐藏修订、未流式文本或失败状态。"""
    output = io.StringIO()
    display = ReplyDisplay(output)
    display.on_event(event("text_delta", text=streamed))
    display.on_event(event("model_reply", final_text=final))
    display.finish(RunResult(status, "run", 1, final_text=final))
    assert final in output.getvalue()
    if streamed == final:
        assert output.getvalue().count(final) == 1 and "完成。" not in output.getvalue()


@pytest.mark.skipif(os.name != "posix", reason="PTY 交互在 POSIX 平台验证")
def test_real_tty_slash_menu_tab_and_exit(tmp_path):
    """真实伪终端输入 / 即展示菜单；Tab 补全 status，最后通过 exit 退出，无模型调用。"""
    import termios

    master, slave = os.openpty()
    termios.tcsetwinsize(slave, (30, 120))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "interfaces",
            "chat",
            "--no-env",
            "--data-dir",
            str(tmp_path / "sessions"),
        ],
        cwd=tmp_path,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"},
    )
    os.close(slave)
    transcript = bytearray()

    def until(marker, timeout=15):
        """按真实输出等待而非固定长 sleep；失败时提供终端片段帮助诊断。"""
        start, deadline = len(transcript), time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in transcript[start:]:
                return
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                transcript.extend(chunk)
        raise AssertionError(transcript.decode(errors="replace"))

    try:
        until(b"pi-claw>")
        os.write(master, b"/")
        until(b"/status")
        assert b"route>" not in transcript
        os.write(master, b"\x15/sta\t\r")
        until("最近结果：".encode())
        os.write(master, b"/exit\r")
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)


def test_repl_undo_restores_last_host_write(tmp_path):
    """`/undo` 只撤销最近一次宿主写入并如实报告，不静默回滚。"""
    (tmp_path / "alpha.txt").write_text("original\n", encoding="utf-8")
    output = io.StringIO()
    provider = ScriptedProvider(
        [
            ModelReply.call(
                ToolCall(
                    "w",
                    "write_or_replace",
                    {"path": "alpha.txt", "old": "original\n", "new": "changed\n"},
                )
            ),
            ModelReply.final("已修改"),
        ]
    )
    repl = CodingRepl(
        workspace=tmp_path,
        provider=provider,
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        output=output,
        auto_approve=True,
        streaming=False,
        max_steps=2,
    )
    repl.run_turn("修改 alpha.txt")
    assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "changed\n"

    assert repl._handle_command("/undo")

    assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "original\n"
    assert "撤销" in output.getvalue()
    # 无更多记录时给出明确说明，而不是静默成功。
    assert repl._handle_command("/undo")
    assert "没有可撤销" in output.getvalue()


def test_cli_rollback_on_cancel_restores_host_writes(tmp_path, monkeypatch):
    """`--rollback-on-cancel` 仅在 cancelled 终态回滚宿主写入。"""
    from dataclasses import replace as dataclass_replace

    import interfaces.cli as cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.txt").write_text("original\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply.call(
                ToolCall(
                    "w",
                    "write_or_replace",
                    {"path": "alpha.txt", "old": "original\n", "new": "changed\n"},
                )
            ),
            ModelReply.final("已修改"),
        ]
    )
    monkeypatch.setattr(cli, "ScriptedProvider", lambda _replies: provider)

    class CancellingAgent(cli.Agent):
        def run(self, task, **kwargs):
            result = super().run(task, **kwargs)
            return dataclass_replace(result, status=RunStatus.CANCELLED)

    monkeypatch.setattr(cli, "Agent", CancellingAgent)
    rc = main(
        [
            "run",
            "修改 alpha.txt",
            "--workspace",
            str(tmp_path),
            "--yes",
            "--rollback-on-cancel",
            "--max-steps",
            "2",
            "--trace",
            str(tmp_path / "trace.jsonl"),
        ]
    )

    assert rc == 1
    assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "original\n"


def test_cli_completed_run_is_not_rolled_back(tmp_path, monkeypatch):
    """没有 cancelled 终态时即使带 --rollback-on-cancel 也保持改动。"""
    import interfaces.cli as cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.txt").write_text("original\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply.call(
                ToolCall(
                    "w",
                    "write_or_replace",
                    {"path": "alpha.txt", "old": "original\n", "new": "changed\n"},
                )
            ),
            ModelReply.final("已修改"),
        ]
    )
    monkeypatch.setattr(cli, "ScriptedProvider", lambda _replies: provider)
    main(
        [
            "run",
            "修改 alpha.txt",
            "--workspace",
            str(tmp_path),
            "--yes",
            "--rollback-on-cancel",
            "--max-steps",
            "2",
            "--trace",
            str(tmp_path / "trace.jsonl"),
        ]
    )

    assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "changed\n"
