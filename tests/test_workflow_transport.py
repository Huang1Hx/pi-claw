"""学校模型配置的 HTTP/SSE 协议联调；仅使用假密钥和网络替身。"""

import io
import json
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

import pytest

from config import load_settings
from core.schemas import ModelReply, ToolCall
from interfaces.cli import main
from providers import OpenAICompatibleProvider, ProviderError
from providers.streaming import parse_message, stream_events
from routing import RoutedProvider

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "pi-claw.ustc.example.toml"
FAKE_ENV = {
    "OPENAI_API_KEY": "fake-school-key",
    "OPENAI_BASE_URL": "https://gateway.invalid/v1",
    "MOONSHOT_API_KEY": "fake-moonshot-key",
    "MOONSHOT_BASE_URL": "https://moonshot.invalid/v1",
}


def frame(delta=None, finish=None, usage=None):
    """构造一帧标准聊天事件；仅最终已知 usage 进入计数。"""
    body = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        body = {"choices": [], "usage": usage}
    return ("data: " + json.dumps(body) + "\n\n").encode()


def response(reply, stream=False):
    """同一模型消息分别经过真实同步解析器或 SSE 聚合器。"""
    message = reply.to_message()
    message.pop("_pi_claw_profile", None)
    usage = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
    finish = "tool_calls" if reply.tool_calls else "stop"
    if stream:
        calls = message.get("tool_calls")
        if calls:
            message["tool_calls"] = [dict(item, index=i) for i, item in enumerate(calls)]
        return io.BytesIO(frame(message, finish) + frame(usage=usage) + b"data: [DONE]\n\n")
    return io.BytesIO(
        json.dumps(
            {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}
        ).encode()
    )


@pytest.mark.parametrize("stream", [False, True])
def test_school_template_cli_end_to_end(monkeypatch, workspace, capsys, stream):
    """低风险真实 CLI 全程使用学校 DeepSeek，不读取 .env 或请求 Kimi。"""
    monkeypatch.chdir(workspace)
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    plan = '{"summary":"检查文件","steps":["读文件"],"verification":["检查内容"]}'
    approve = '{"verdict":"approve","summary":"文件内容与证据一致","issues":[]}'
    scripts = {
        "deepseek-flash": [
            ModelReply.final('{"complexity":"complex"}'),
            ModelReply(final_text=plan, reasoning_content="planner-private"),
            ModelReply(
                tool_calls=(ToolCall("r", "read_file", {"path": "alpha.txt"}),),
                reasoning_content="coder-private",
            ),
            ModelReply.final("检查完成"),
            ModelReply.final(approve),
        ],
    }
    requests = []

    def open_fake(request, timeout):
        """验证共用授权和 endpoint，只改变模型与该模型的推理参数。"""
        payload = json.loads(request.data)
        assert request.get_header("Authorization") == "Bearer fake-school-key"
        assert request.full_url == "https://gateway.invalid/v1/chat/completions"
        requests.append((deepcopy(payload), timeout))
        return response(scripts[payload["model"]].pop(0), payload.get("stream", False))

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    args = [
        "chat",
        "检查 alpha.txt",
        "--workspace",
        str(workspace),
        "--print",
        "--no-env",
        "--data-dir",
        str(workspace / "sessions"),
        "--trace",
        str(workspace / "trace.jsonl"),
        "--config",
        str(TEMPLATE),
        "--route",
        "auto",
        "--max-steps",
        "24",
    ]
    if not stream:
        args.append("--no-stream")
    assert main(args) == 0
    assert [p["model"] for p, _ in requests] == ["deepseek-flash"] * 5
    assert requests[0][0]["max_tokens"] == 128
    assert "chat_template_kwargs" not in requests[0][0]
    assert all(t == 120 for _, t in requests)
    coder = [p for p, _ in requests if p.get("thinking") == {"type": "enabled"}]
    assert len(coder) == 2
    assert coder[0]["thinking"] == {"type": "enabled"}
    assert coder[0].get("stream", False) is stream
    assert "coder-private" in repr(coder[1]["messages"])
    assert "coder-private" not in repr(requests[-1])
    assert "planner-private" not in repr(coder)
    assert "_pi_claw_profile" not in repr(requests)
    disk = "".join(p.read_text() for p in workspace.rglob("*.json*"))
    display = capsys.readouterr().out
    assert "规划" in display and "审查" in display
    assert display.count("检查完成") == 1
    assert display.count("文件内容与证据一致") == 1
    for marker in ("planner-private", "coder-private", "fake-school-key"):
        assert marker not in disk + display
    assert '"workflow_artifact"' in disk
    assert '"requests_by_purpose"' in disk


@pytest.mark.parametrize(
    "old,new",
    [
        ("enabled = true", 'enabled = "yes"'),
        ('planner = "planner"', 'planner = "missing"'),
        ("max_planning_requests = 4", "max_planning_requests = 0"),
        ("max_review_requests = 4", "max_review_requests = true"),
        ("max_premium_review_requests = 4", "max_premium_review_requests = 0"),
        ("max_review_rounds = 3", "max_review_rounds = 6"),
        ("max_context_chars = 120000", "max_context_chars = 100"),
        ('thinking_mode = "enabled"', "thinking_mode = []"),
        ('thinking_format = "thinking"', 'thinking_format = "wrong"'),
        ("max_output_tokens = 2048", "max_output_tokens = false"),
        ("timeout_seconds = 120", "timeout_seconds = 301"),
        ("max_attempts = 1", "max_attempts = 0"),
        ('premium_review_policy = "risk"', 'premium_review_policy = "sometimes"'),
        ("[workflow]", '[workflow]\nunknown = "forbidden"'),
    ],
)
def test_invalid_workflow_configuration_rejected(tmp_path, old, new):
    """角色、参数与预算错误在网络调用前报告。"""
    original = TEMPLATE.read_text()
    assert old in original
    path = tmp_path / "bad.toml"
    path.write_text(original.replace(old, new))
    with pytest.raises(ValueError):
        load_settings(config_path=path, no_env=True, environ=FAKE_ENV)


def test_template_roles_and_per_profile_limits():
    """普通角色共用学校 DeepSeek，Kimi 使用独立连接和单次传输尝试。"""
    cfg = load_settings(config_path=TEMPLATE, no_env=True, environ=FAKE_ENV)
    assert cfg.workflow.enabled
    normal_roles = (
        cfg.routing.cheap,
        cfg.routing.strong,
        cfg.workflow.planner,
        cfg.workflow.coder,
        cfg.workflow.reviewer,
    )
    assert {cfg.profiles[name].model for name in normal_roles} == {"deepseek-flash"}
    assert {cfg.profiles[name].api_key for name in normal_roles} == {FAKE_ENV["OPENAI_API_KEY"]}
    premium = cfg.profiles[cfg.workflow.premium_reviewer]
    assert premium.model == "kimi-k3"
    assert premium.base_url == FAKE_ENV["MOONSHOT_BASE_URL"]
    assert premium.api_key == FAKE_ENV["MOONSHOT_API_KEY"]
    assert premium.max_attempts == 1
    assert {profile.model for profile in cfg.profiles.values()} == {
        "deepseek-flash",
        "kimi-k3",
    }
    assert cfg.routing.max_requests == 36
    assert "fake-school-key" not in repr(cfg) and "fake-moonshot-key" not in repr(cfg)


def test_independent_premium_transport_uses_own_connection_and_one_attempt(tmp_path, monkeypatch):
    """Kimi 不能携带学校 Key，暂时性失败也不能自动产生第二次付费请求。"""
    config = tmp_path / "connections.toml"
    config.write_text(
        """
[connection]
base_url_env = "SCHOOL_URL"
api_key_env = "SCHOOL_KEY"

[models.primary]
model = "deepseek-flash"

[models.premium_reviewer]
base_url_env = "MOONSHOT_URL"
api_key_env = "MOONSHOT_KEY"
model = "kimi-k3"
max_attempts = 1
""",
        encoding="utf-8",
    )
    environment = {
        "SCHOOL_URL": "https://school.invalid/v1",
        "SCHOOL_KEY": "fake-school-secret",
        "MOONSHOT_URL": "https://moonshot.invalid/v1",
        "MOONSHOT_KEY": "fake-moonshot-secret",
    }
    settings = load_settings(config_path=config, environ=environment, no_env=True)
    requests = []

    def open_fake(request, **_kwargs):
        requests.append(
            (
                request.full_url,
                request.get_header("Authorization"),
                json.loads(request.data)["model"],
            )
        )
        if "moonshot.invalid" in request.full_url:
            raise urllib.error.URLError("temporary")
        return response(ModelReply.final("READY"))

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    reply = RoutedProvider(settings, mode="primary").complete([{"role": "user", "content": "go"}])
    assert reply.final_text == "READY"
    with pytest.raises(ProviderError):
        RoutedProvider(settings, mode="premium_reviewer").complete(
            [{"role": "user", "content": "review"}]
        )
    assert requests == [
        (
            "https://school.invalid/v1/chat/completions",
            "Bearer fake-school-secret",
            "deepseek-flash",
        ),
        (
            "https://moonshot.invalid/v1/chat/completions",
            "Bearer fake-moonshot-secret",
            "kimi-k3",
        ),
    ]
    assert "fake-school-secret" not in repr(settings)
    assert "fake-moonshot-secret" not in repr(settings)


@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("thinking", {"thinking": {"type": "disabled"}}),
        ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("enable_thinking", {"enable_thinking": False}),
    ],
)
def test_thinking_switch_formats_and_message_ownership(monkeypatch, fmt, expected):
    """只透传固定参数白名单；不同 profile 的推理不发送，且不改写原始历史。"""
    messages = [
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_content": "same-role",
            "_pi_claw_profile": "primary",
        },
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_content": "foreign-role",
            "_pi_claw_profile": "other",
        },
    ]
    before = deepcopy(messages)
    bodies = []

    def open_fake(request, **kwargs):
        """捕获真实序列化请求，不使用服务端。"""
        bodies.append(json.loads(request.data))
        return response(ModelReply.final("done"))

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    provider = OpenAICompatibleProvider(
        api_key="fake-key",
        base_url="https://gateway.invalid/v1",
        model="fake-model",
        profile_name="primary",
        thinking_mode="disabled",
        thinking_format=fmt,
    )
    provider.complete(messages)
    assert all(bodies[0][k] == v for k, v in expected.items())
    assert "same-role" in repr(bodies[0]) and "foreign-role" not in repr(bodies[0])
    assert "_pi_claw_profile" not in repr(bodies[0]) and messages == before


def test_reasoning_stream_kept_private_and_rejects_invalid_tail():
    """思考片段不可混作用户正文，finish 后继续发思考同样是协议错误。"""
    raw = frame({"reasoning_content": "part1"}) + frame({"reasoning_content": "part2"})
    raw += frame({"content": "answer"}, "stop") + b"data: [DONE]\n\n"
    events = list(stream_events(io.BytesIO(raw), 50000))
    assert events[-1].reply.reasoning_content == "part1part2"
    assert [e.data["text"] for e in events if e.type == "text_delta"] == ["answer"]
    assert "part1" not in repr(events[-1].reply) + repr(events[-1].reply.to_dict())
    bad = frame({"content": "answer"}, "stop") + frame({"reasoning_content": "too-late"})
    with pytest.raises(ProviderError):
        list(stream_events(io.BytesIO(bad + b"data: [DONE]\n\n"), 50000))


def test_partial_reasoning_retry_is_bounded_and_does_not_publish_thoughts(monkeypatch):
    """思考流中断后最多重试配置次数；私有推理仍不得进入正文。"""
    attempts = []

    class Interrupted(io.BytesIO):
        """在第一个思考帧后模拟网络中断。"""

        def readline(self, size=-1):
            """耗尽已有片段后抛传输错误。"""
            value = super().readline(size)
            if not value:
                raise TimeoutError("interrupted")
            return value

    def open_fake(*args, **kwargs):
        """统计真正的传输尝试次数。"""
        attempts.append(1)
        return Interrupted(frame({"reasoning_content": "private"}))

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    with pytest.raises(ProviderError):
        list(
            OpenAICompatibleProvider(api_key="fake", model="fake", sleep=lambda _: None).stream([])
        )
    assert len(attempts) == 3


@pytest.mark.parametrize("thought", [False, {}, ["bad"]])
def test_invalid_reasoning_rejected(thought):
    """推理字段只能是文本或空，不允许结构混淆。"""
    with pytest.raises(ProviderError):
        parse_message({"content": "done", "reasoning_content": thought})
    with pytest.raises(ProviderError):
        list(
            stream_events(
                io.BytesIO(frame({"reasoning_content": thought}, "stop") + b"data: [DONE]\n\n"),
                50000,
            )
        )
