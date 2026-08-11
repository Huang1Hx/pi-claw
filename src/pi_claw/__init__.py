"""Pi-Claw：可审计、可回放、默认只读的本地 Coding Agent。"""

from .agent import Agent, AgentRunner
from .approval import ApprovalChoice, TerminalApprovalBroker
from .context import ContextManager
from .policy import WorkspacePolicy
from .prompts import build_system_prompt, discover_project_context
from .providers import (
    OfflineCodingProvider,
    OpenAICompatibleProvider,
    Provider,
    ProviderError,
    ScriptedProvider,
)
from .schemas import (
    ModelReply,
    PolicyAction,
    PolicyDecision,
    RunResult,
    RunStatus,
    ToolCall,
    ToolResult,
)
from .session import SessionInfo, SessionStore
from .tools import ToolExecutor, ToolRegistry
from .trace import TraceRecorder, TraceReplay, TraceWriter, redact, replay_trace
from .workspace import Workspace, WorkspaceViolation

__all__ = [
    "Agent",
    "AgentRunner",
    "ApprovalChoice",
    "ContextManager",
    "ModelReply",
    "OfflineCodingProvider",
    "OpenAICompatibleProvider",
    "PolicyAction",
    "PolicyDecision",
    "Provider",
    "ProviderError",
    "RunResult",
    "RunStatus",
    "ScriptedProvider",
    "SessionInfo",
    "SessionStore",
    "TerminalApprovalBroker",
    "ToolCall",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "TraceRecorder",
    "TraceReplay",
    "TraceWriter",
    "Workspace",
    "WorkspacePolicy",
    "WorkspaceViolation",
    "build_system_prompt",
    "discover_project_context",
    "redact",
    "replay_trace",
]
