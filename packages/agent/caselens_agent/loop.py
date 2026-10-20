"""The investigator agent loop: plan -> act -> observe -> reflect, then synthesize + validate.

The loop talks to the model only through `ModelRouter.complete_json` with Pydantic-validated
JSON actions (task "plan" per step, "synthesize" for the answer). Tool results go back to the
model as `evidence_block(...)`-wrapped JSON with source handles; the runtime expands the handles
the model cites into contract citations and runs the validator. One corrective retry, then the
answer falls back to `insufficient_evidence`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from caselens_agent.prompts import (
    AgentAction,
    DraftAnswer,
    DraftCitation,
    plan_system,
    synth_system,
)
from caselens_agent.router import ModelError, ModelRouter, evidence_block
from caselens_agent.sources import SourceLedger, fit_observation
from caselens_agent.tools import ToolOutcome, ToolRuntime
from caselens_agent.validator import insufficient_evidence, validate_answer


@dataclass(frozen=True)
class AgentConfig:
    max_tool_calls: int = 8
    # Rough budget for the model-facing transcript (~4 chars per token, ~40k input tokens).
    max_transcript_chars: int = 120_000
    max_observation_chars: int = 8_000
    validator_retries: int = 1


@dataclass
class ToolCallRecord:
    """One tool call as stored in agent_runs.tool_calls (no evidence text)."""

    step: int
    tool: str
    input: Any
    ok: bool
    error: str | None
    result_count: int
    sources: list[str]
    duration_ms: int
    sql: str | None = None
    conflict_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "input": self.input,
            "ok": self.ok,
            "error": self.error,
            "result_count": self.result_count,
            "sources": self.sources,
            "duration_ms": self.duration_ms,
            "sql": self.sql,
            "conflict_id": self.conflict_id,
        }


@dataclass
class LoopResult:
    answer: dict[str, Any]
    tool_calls: list[ToolCallRecord]
    thoughts: list[str]
    ledger: SourceLedger
    validation_errors: list[list[str]] = field(default_factory=list)
    fell_back: bool = False
    model_failed: bool = False
    stop_reason: str = "finish"


def _render_step(step: int, action: AgentAction, outcome: ToolOutcome, limit: int) -> str:
    call = json.dumps({"tool": action.tool, "input": action.input}, ensure_ascii=False)
    head = f"Step {step}: you called {call}"
    if not outcome.ok:
        return f"{head}\nTool error: {outcome.error}"
    body = fit_observation(outcome.observation or {}, limit)
    return f"{head}\n" + evidence_block(f"{outcome.tool} result, step {step}", body)


def compose_answer(draft: DraftAnswer, ledger: SourceLedger) -> tuple[dict[str, Any], list[str]]:
    """Expand source / flag handles into a contract answer. Returns (answer, expansion errors)."""
    errors: list[str] = []
    citations: list[dict[str, Any]] = []
    index: dict[tuple[str, str | None], str] = {}

    def cite(where: str, items: list[DraftCitation]) -> list[str]:
        ids: list[str] = []
        for item in items:
            observed = ledger.get(item.source)
            if observed is None:
                errors.append(f"{where} cites unknown source handle {item.source!r}")
                continue
            quote = (item.quote or "").strip() or None
            key = (observed.handle, quote)
            cid = index.get(key)
            if cid is None:
                cid = f"c{len(citations) + 1}"
                index[key] = cid
                ref = observed.ref
                citations.append(
                    {
                        "id": cid,
                        "source_type": ref.source_type,
                        "evidence_id": ref.evidence_id,
                        "chunk_id": ref.chunk_id,
                        "table": ref.table,
                        "record_id": ref.record_id,
                        "locator": ref.locator,
                        "quote": quote[:1000] if quote else None,
                    }
                )
            if cid not in ids:
                ids.append(cid)
        return ids

    claims = []
    for i, c in enumerate(draft.claims, start=1):
        claims.append(
            {
                "id": f"cl{i}",
                "text": c.text,
                "citation_ids": cite(f"claim cl{i}", c.citations),
                "t_start": c.t_start or None,
                "t_end": c.t_end or None,
            }
        )
    conflicts = []
    for i, cf in enumerate(draft.conflicts, start=1):
        record_id = None
        if cf.flag:
            flagged = ledger.flagged.get(cf.flag.strip().upper())
            if flagged is None:
                errors.append(f"conflict cf{i} names unknown flag {cf.flag!r}")
            else:
                record_id = flagged.conflict_id
        conflicts.append(
            {
                "id": f"cf{i}",
                "description": cf.description,
                "claim_a": {
                    "text": cf.claim_a.text,
                    "citation_ids": cite(f"conflict cf{i} claim_a", cf.claim_a.citations),
                },
                "claim_b": {
                    "text": cf.claim_b.text,
                    "citation_ids": cite(f"conflict cf{i} claim_b", cf.claim_b.citations),
                },
                "conflict_record_id": record_id,
            }
        )
    answer = {
        "status": draft.status,
        "answer": draft.answer,
        "claims": claims,
        "citations": citations,
        "conflicts": conflicts,
        "confidence": draft.confidence,
        "open_questions": [q for q in draft.open_questions if q.strip()],
    }
    return answer, errors


class AgentLoop:
    def __init__(
        self,
        router: ModelRouter,
        runtime: ToolRuntime,
        config: AgentConfig | None = None,
        on_tool_call: Callable[[ToolCallRecord], None] | None = None,
    ) -> None:
        self.router = router
        self.runtime = runtime
        self.config = config or AgentConfig()
        self.on_tool_call = on_tool_call

    def _transcript(self, question: str, steps: list[str]) -> str:
        # The question may not open or close evidence blocks.
        safe = question.replace("<<<", "< < <").replace(">>>", "> > >")
        parts = [f"Question from the investigator:\n{safe}"]
        if steps:
            parts.append("Tool calls so far:")
            parts.extend(steps)
        else:
            parts.append("No tools called yet.")
        return "\n\n".join(parts)

    def run(self, question: str) -> LoopResult:
        cfg = self.config
        ledger = self.runtime.ledger
        steps: list[str] = []
        records: list[ToolCallRecord] = []
        thoughts: list[str] = []
        stop_reason = "max_tool_calls"
        system = plan_system(cfg.max_tool_calls)

        while len(records) < cfg.max_tool_calls:
            transcript = self._transcript(question, steps)
            if len(transcript) > cfg.max_transcript_chars:
                stop_reason = "budget"
                break
            remaining = cfg.max_tool_calls - len(records)
            user = f"{transcript}\n\nTool calls left: {remaining}. Reply with your next action."
            try:
                action = self.router.complete_json("plan", system, user, AgentAction)
            except ModelError:
                stop_reason = "model_error"
                break
            thoughts.append(action.thought)
            if action.action == "finish":
                stop_reason = "finish"
                break
            step = len(records) + 1
            tool = action.tool or ""
            outcome = self.runtime.call(tool, action.input if action.input is not None else {})
            output = outcome.output or {}
            record = ToolCallRecord(
                step=step,
                tool=tool,
                input=action.input,
                ok=outcome.ok,
                error=outcome.error,
                result_count=outcome.result_count,
                sources=outcome.handles,
                duration_ms=outcome.duration_ms,
                sql=output.get("sql") if tool == "query_records" else None,
                conflict_id=output.get("conflict_id") if tool == "flag_conflict" else None,
            )
            records.append(record)
            steps.append(_render_step(step, action, outcome, cfg.max_observation_chars))
            if self.on_tool_call is not None:
                self.on_tool_call(record)

        answer, errors_log, fell_back, model_failed = self._synthesize(question, steps, ledger)
        return LoopResult(
            answer=answer,
            tool_calls=records,
            thoughts=thoughts,
            ledger=ledger,
            validation_errors=errors_log,
            fell_back=fell_back,
            model_failed=model_failed,
            stop_reason=stop_reason,
        )

    def _synthesize(
        self, question: str, steps: list[str], ledger: SourceLedger
    ) -> tuple[dict[str, Any], list[list[str]], bool, bool]:
        transcript = self._transcript(question, steps)
        handles = ", ".join(ledger.handles()) or "(none)"
        flags = ", ".join(f"{f.handle} ({f.description[:120]})" for f in ledger.flagged.values())
        base = (
            f"{transcript}\n\nCitable source handles: {handles}\n"
            f"Conflicts recorded with flag_conflict: {flags or '(none)'}\n\n"
            "Write the final answer JSON now."
        )
        errors_log: list[list[str]] = []
        model_errors = 0
        attempts = 1 + self.config.validator_retries
        for _ in range(attempts):
            user = base
            if errors_log:
                listed = "\n".join(f"- {e}" for e in errors_log[-1][:15])
                user = (
                    f"{base}\n\nYour previous answer was rejected by the citation validator:\n"
                    f"{listed}\nFix every error. Drop any claim you cannot cite."
                )
            try:
                draft = self.router.complete_json("synthesize", synth_system(), user, DraftAnswer)
            except ModelError as exc:
                model_errors += 1
                errors_log.append([f"model error: {exc}"[:300]])
                continue
            answer, errors = compose_answer(draft, ledger)
            errors += validate_answer(answer, ledger)
            if not errors:
                return answer, errors_log, False, False
            errors_log.append(errors)
        reason = (
            "The model could not produce an answer."
            if model_errors == attempts
            else "The drafted answer failed citation validation twice, so it was withheld: "
            "every claim must cite a source returned by the tools in this run."
        )
        fallback = insufficient_evidence(reason, ledger)
        return fallback, errors_log, True, model_errors == attempts
