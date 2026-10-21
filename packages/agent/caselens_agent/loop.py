"""The investigator agent loop: plan -> act -> observe -> reflect, then synthesize + validate.

Fast path first: before any model call the runtime runs `prefetch_calls(question)` (search the
evidence and the video for the question, look up the names / plates / phones it mentions), so
the first model turn already sees observations. Each turn the model returns a Pydantic-validated
JSON action: more tool calls, `finish` (a separate "synthesize" call writes the answer), or
`answer` with the final draft itself (saves that call). Tool results go back to the model as
`evidence_block(...)`-wrapped JSON with source handles; the runtime expands the handles the model
cites into contract citations and runs the validator on every draft, direct or synthesized. One
corrective retry in total, then the answer falls back to `insufficient_evidence`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from caselens_agent.crosscheck import cross_check_note
from caselens_agent.prefetch import names_records, prefetch_calls
from caselens_agent.prompts import (
    AgentAction,
    DraftAnswer,
    DraftCitation,
    PlannedCall,
    plan_system,
    synth_system,
)
from caselens_agent.router import ModelError, ModelRouter, Task, UsageMeter, evidence_block
from caselens_agent.sources import SourceLedger, fit_observation, normalize_ws
from caselens_agent.tools import ToolOutcome, ToolRuntime
from caselens_agent.validator import insufficient_evidence, validate_answer


@dataclass(frozen=True)
class AgentConfig:
    max_tool_calls: int = 8
    # Wall-clock times in the evidence are local to the case (env CASELENS_CASE_TIMEZONE).
    case_timezone: str = field(
        default_factory=lambda: os.environ.get("CASELENS_CASE_TIMEZONE") or "America/Los_Angeles"
    )
    # Rough budget for the model-facing transcript (~4 chars per token, ~40k input tokens).
    max_transcript_chars: int = 120_000
    max_observation_chars: int = 8_000
    validator_retries: int = 1
    # Retrieval-first fast path: tool calls run before the first model turn (they count toward
    # max_tool_calls), with up to `prefetch_entities` get_entity lookups.
    prefetch: bool = True
    prefetch_entities: int = 2
    # Also prefetch the record operation (query_records) for the phones / plates / item tags a
    # question names, windowed by the dates and times it names.
    prefetch_records: bool = True
    # Router task for the first turn after a prefetch (it often writes the answer directly).
    first_turn_task: Task = "synthesize"
    # Once per run, a direct answer that still lists open questions while tool calls remain is
    # sent back once: "call a tool that settles them, or repeat the answer".
    follow_open_questions: bool = True
    # Append the deterministic cross-check of observed sources (crosscheck.py) to the transcript.
    cross_check: bool = True


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
    usage: UsageMeter = field(default_factory=UsageMeter)
    prefetched_steps: int = 0
    quotes_dropped: int = 0


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _call_key(tool: str, tool_input: Any) -> str:
    return json.dumps({"tool": tool, "input": tool_input}, ensure_ascii=False, sort_keys=True)


def _render_step(
    step: int, call_obj: PlannedCall, outcome: ToolOutcome, limit: int, auto: bool = False
) -> str:
    call = json.dumps({"tool": call_obj.tool, "input": call_obj.input}, ensure_ascii=False)
    who = "the system ran (before your first turn)" if auto else "you called"
    head = f"Step {step}: {who} {call}"
    if not outcome.ok:
        # Error text can echo values the model wrote or a backend saw: data, never instructions.
        return f"{head}\nTool error:\n" + evidence_block(
            f"{outcome.tool} error, step {step}", outcome.error or "unknown error"
        )
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
            elif flagged.stored:
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


# Quotation marks and ellipses a model may wrap around a quote (ASCII and typographic).
_QUOTE_MARKS = "'." + "".join(map(chr, (0x22, 0x201C, 0x201D, 0x2018, 0x2019, 0x2026)))


def repair_quotes(draft: DraftAnswer, ledger: SourceLedger) -> int:
    """Null out quotes that are not verbatim excerpts of their source, in place; returns how many.

    Only the optional quote text goes: the citation (the source handle) stays and is validated as
    usual, and no unverified quote ever reaches the answer. Quotes that only differ by wrapping
    quotation marks or ellipses are trimmed to the verbatim part instead. This saves a corrective
    model call for the most common slip (a paraphrased "quote").
    """
    dropped = 0
    items: list[DraftCitation] = [c for claim in draft.claims for c in claim.citations]
    for cf in draft.conflicts:
        items += cf.claim_a.citations + cf.claim_b.citations
    for item in items:
        if not item.quote or not item.quote.strip():
            continue
        observed = ledger.get(item.source)
        if observed is None:
            continue  # an unknown handle is reported by compose_answer
        texts = [normalize_ws(t) for t in observed.texts]
        trimmed = item.quote.strip().strip(_QUOTE_MARKS).strip()
        for candidate in (item.quote, trimmed):
            needle = normalize_ws(candidate)
            if needle and any(needle in t for t in texts):
                item.quote = candidate
                break
        else:
            item.quote = None
            dropped += 1
    return dropped


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
        tz = self.config.case_timezone
        parts = [
            f"Question from the investigator:\n{safe}",
            f"Case timezone: {tz}. Dates and times in the question and the evidence are local to "
            f"it; write tool time filters with that local UTC offset (e.g. a whole local day "
            f"2026-03-14T00:00:00-07:00 to 2026-03-15T00:00:00-07:00), never a bare Z day.",
        ]
        if steps:
            parts.append("Tool calls so far:")
            parts.extend(steps)
            note = cross_check_note(self.runtime.ledger) if self.config.cross_check else None
            if note:
                parts.append(note)
        else:
            parts.append("No tools called yet.")
        return "\n\n".join(parts)

    def _execute(
        self,
        call_obj: PlannedCall,
        records: list[ToolCallRecord],
        steps: list[str],
        auto: bool = False,
    ) -> None:
        step = len(records) + 1
        outcome = self.runtime.call(call_obj.tool, call_obj.input)
        output = outcome.output or {}
        record = ToolCallRecord(
            step=step,
            tool=call_obj.tool,
            input=call_obj.input,
            ok=outcome.ok,
            error=outcome.error,
            result_count=outcome.result_count,
            sources=outcome.handles,
            duration_ms=outcome.duration_ms,
            sql=output.get("sql") if call_obj.tool == "query_records" else None,
            # A demo user's flag_conflict stores nothing: no record id to log.
            conflict_id=output.get("conflict_id")
            if call_obj.tool == "flag_conflict" and self.runtime.ctx.persist_conflicts
            else None,
        )
        records.append(record)
        steps.append(
            _render_step(step, call_obj, outcome, self.config.max_observation_chars, auto=auto)
        )
        if self.on_tool_call is not None:
            self.on_tool_call(record)

    def run(self, question: str) -> LoopResult:
        cfg = self.config
        ledger = self.runtime.ledger
        meter = UsageMeter()
        steps: list[str] = []
        records: list[ToolCallRecord] = []
        thoughts: list[str] = []
        stop_reason = "max_tool_calls"
        system = plan_system(cfg.max_tool_calls)
        seen: dict[str, int] = {}
        repeats = 0
        direct: DraftAnswer | None = None

        if cfg.prefetch:
            tz = _zone(cfg.case_timezone)
            year: int | None = None
            if cfg.prefetch_records and names_records(question):
                year = self.runtime.case_year() or datetime.now(tz).year
            planned = prefetch_calls(question, cfg.prefetch_entities, tz=tz, year=year)
            for call_obj in planned[: cfg.max_tool_calls]:
                seen[_call_key(call_obj.tool, call_obj.input)] = len(records) + 1
                self._execute(call_obj, records, steps, auto=True)
        prefetched = len(records)

        turn = 0
        nudge = ""  # one-off note for the next turn
        nudged: DraftAnswer | None = None  # the direct answer that was sent back, as a fallback
        nudged_at = 0  # tool calls made when it was sent back
        while len(records) < cfg.max_tool_calls:
            transcript = self._transcript(question, steps)
            if len(transcript) > cfg.max_transcript_chars:
                stop_reason = "budget"
                break
            remaining = cfg.max_tool_calls - len(records)
            user = f"{transcript}\n\nTool calls left: {remaining}. Reply with your next action."
            if nudge:
                user, nudge = f"{user}\n\n{nudge}", ""
            # The first turn often writes the answer itself, so it gets the synthesis model
            # settings; later turns only pick tools and use the faster planning settings.
            task: Task = cfg.first_turn_task if turn == 0 and prefetched else "plan"
            turn += 1
            try:
                action = self.router.complete_json(task, system, user, AgentAction, meter=meter)
            except ModelError:
                stop_reason = "model_error"
                break
            thoughts.append(action.thought)
            if action.action == "answer" and action.answer is not None:
                draft = action.answer
                gaps = [q.strip() for q in draft.open_questions if q.strip()]
                if (
                    cfg.follow_open_questions
                    and nudged is None
                    and draft.status == "answered"
                    and gaps
                ):
                    nudged, nudged_at = draft, len(records)
                    listed = "\n".join(f"- {g[:200]}" for g in gaps[:5])
                    nudge = (
                        "Your draft answer leaves these open:\n"
                        + evidence_block("open questions of your draft", listed)
                        + "\nIf a tool call you have not tried could settle one (follow names, "
                        "numbers, plates, places or receipts in the observations), call it now. "
                        "Otherwise reply with the same answer."
                    )
                    continue
                direct = draft
                stop_reason = "answer"
                break
            planned = action.planned_calls()
            if action.action != "call_tool" or not planned:
                stop_reason = "finish"
                break
            fresh = False
            for call_obj in planned:
                if len(records) >= cfg.max_tool_calls:
                    break
                key = _call_key(call_obj.tool, call_obj.input)
                if key in seen:
                    steps.append(
                        f"Repeated call {key} skipped: it already ran as step {seen[key]}; "
                        "use that result."
                    )
                    continue
                fresh = True
                seen[key] = len(records) + 1
                self._execute(call_obj, records, steps)
            if not fresh:
                repeats += 1
                if repeats >= 2:  # the model keeps repeating itself: stop and answer
                    stop_reason = "repeated_calls"
                    break

        if direct is None and nudged is not None and len(records) == nudged_at:
            # Nothing new was observed after the nudge (finish, model error): keep that draft.
            direct = nudged
            stop_reason = "answer"
        errors_log: list[list[str]] = []
        quotes_dropped = 0
        answer: dict[str, Any] | None = None
        fell_back = model_failed = False
        if direct is not None:
            quotes_dropped = repair_quotes(direct, ledger)
            composed, errors = compose_answer(direct, ledger)
            errors += validate_answer(composed, ledger)
            if errors:
                errors_log.append(errors)  # counts as the first synthesis attempt
            else:
                answer = composed
        if answer is None:
            answer, errors_log, fell_back, model_failed, dropped = self._synthesize(
                question, steps, ledger, meter, errors_log
            )
            quotes_dropped += dropped
        return LoopResult(
            answer=answer,
            tool_calls=records,
            thoughts=thoughts,
            ledger=ledger,
            validation_errors=errors_log,
            fell_back=fell_back,
            model_failed=model_failed,
            stop_reason=stop_reason,
            usage=meter,
            prefetched_steps=prefetched,
            quotes_dropped=quotes_dropped,
        )

    def _synthesize(
        self,
        question: str,
        steps: list[str],
        ledger: SourceLedger,
        meter: UsageMeter,
        prior_errors: list[list[str]],
    ) -> tuple[dict[str, Any], list[list[str]], bool, bool, int]:
        transcript = self._transcript(question, steps)
        handles = ", ".join(ledger.handles()) or "(none)"
        flags = "\n".join(f"{f.handle}: {f.description[:120]}" for f in ledger.flagged.values())
        flagged = (
            evidence_block("conflicts recorded with flag_conflict (handle: description)", flags)
            if flags
            else "(none)"
        )
        base = (
            f"{transcript}\n\nCitable source handles: {handles}\n"
            f"Conflicts recorded with flag_conflict: {flagged}\n\n"
            "Write the final answer JSON now."
        )
        errors_log = list(prior_errors)
        model_errors = 0
        dropped = 0
        # A rejected direct answer already used the first attempt.
        attempts = max(1, 1 + self.config.validator_retries - len(prior_errors))
        for _ in range(attempts):
            user = base
            if errors_log:
                listed = "\n".join(f"- {e}" for e in errors_log[-1][:15])
                user = (
                    f"{base}\n\nYour previous answer was rejected by the citation validator:\n"
                    + evidence_block("validator errors", listed)
                    + "\nFix every error. Drop any claim you cannot cite."
                )
            try:
                draft = self.router.complete_json(
                    "synthesize", synth_system(), user, DraftAnswer, meter=meter
                )
            except ModelError as exc:
                model_errors += 1
                errors_log.append([f"model error: {exc}"[:300]])
                continue
            dropped += repair_quotes(draft, ledger)
            answer, errors = compose_answer(draft, ledger)
            errors += validate_answer(answer, ledger)
            if not errors:
                return answer, errors_log, False, False, dropped
            errors_log.append(errors)
        reason = (
            "The model could not produce an answer."
            if model_errors == attempts and not prior_errors
            else "The drafted answer failed citation validation twice, so it was withheld: "
            "every claim must cite a source returned by the tools in this run."
        )
        failed = model_errors == attempts and not prior_errors
        fallback = insufficient_evidence(reason, ledger)
        return fallback, errors_log, True, failed, dropped
