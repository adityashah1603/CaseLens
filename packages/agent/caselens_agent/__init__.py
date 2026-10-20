"""CaseLens investigator agent: tools, loop, answer validator, model router."""

from caselens_agent.loop import AgentConfig, AgentLoop, LoopResult, ToolCallRecord, compose_answer
from caselens_agent.runner import AgentResult, CaseAccessError, ask
from caselens_agent.sources import SourceLedger, SourceRef
from caselens_agent.tools import (
    ToolBackends,
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolRuntime,
    default_backends,
)
from caselens_agent.validator import insufficient_evidence, validate_answer

__version__ = "0.0.1"

__all__ = [
    "AgentConfig",
    "AgentLoop",
    "AgentResult",
    "CaseAccessError",
    "LoopResult",
    "SourceLedger",
    "SourceRef",
    "ToolBackends",
    "ToolCallRecord",
    "ToolContext",
    "ToolError",
    "ToolOutcome",
    "ToolRuntime",
    "__version__",
    "ask",
    "compose_answer",
    "default_backends",
    "insufficient_evidence",
    "validate_answer",
]
