"""Pi-Claw：可审计、可回放、默认只读的本地 Coding Agent。"""

from .agent import Agent, AgentRunner
from .approval import ApprovalChoice, TerminalApprovalBroker
from .completion import (
    CompletionCheck,
    CompletionVerifier,
    ToolEvidenceCompletionVerifier,
    ToolObservation,
)
from .context import ContextManager
from .eval import (
    EvalAssertion,
    EvalCaseResult,
    EvalReport,
    EvalRunner,
    default_suite_path,
    load_eval_suite,
    write_eval_report,
)
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
    "CompletionCheck",
    "CompletionVerifier",
    "EvalAssertion",
    "EvalCaseResult",
    "EvalReport",
    "EvalRunner",
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
    "ToolEvidenceCompletionVerifier",
    "ToolObservation",
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
    "default_suite_path",
    "discover_project_context",
    "load_eval_suite",
    "redact",
    "replay_trace",
    "write_eval_report",
]
