"""`ask()`: one audited agent run for one question on one case.

Writes go through the caller's `caselens_app` connection (with `app.user_id` set), so RLS
decides whether the user may run on the case at all:

* start: INSERT agent_runs (status 'running') + audit `agent.ask`
* each tool call: UPDATE agent_runs.tool_calls (allowed by migration 0002 while the run is
  'running') + audit `agent.tool_call` (tool, input, counts; never evidence text)
* end: UPDATE agent_runs with answer, citations, status, model, plan, latency, and token usage
  (input_tokens / output_tokens summed over the run's model calls; cost_usd is 0 on the free
  tier, else the list-price estimate). plan.usage keeps the per-call breakdown and the
  list-price estimate (`list_price_usd_estimate`, for reporting only).

`query_records` runs on the separate `caselens_readonly` connection.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from caselens_agent.loop import AgentConfig, AgentLoop, LoopResult, ToolCallRecord
from caselens_agent.router import ModelRouter
from caselens_agent.tools import Conn, ToolBackends, ToolContext, ToolRuntime, default_backends

AUDIT_INPUT_STRING_LIMIT = 300


class CaseAccessError(PermissionError):
    """The user may not run the agent on this case (RLS rejected the run)."""


@dataclass
class AgentResult:
    run_id: uuid.UUID
    status: str  # agent_runs.status: succeeded | failed
    answer: dict[str, Any]
    tool_calls: list[ToolCallRecord]
    thoughts: list[str]
    latency_ms: int
    model: str
    stop_reason: str
    fell_back: bool
    validation_errors: list[list[str]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


def _clip_strings(value: Any, limit: int = AUDIT_INPUT_STRING_LIMIT) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "..."
    if isinstance(value, dict):
        return {str(k): _clip_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_strings(v, limit) for v in value]
    return value


def _audit(conn: Conn, user_id: uuid.UUID, case_id: uuid.UUID, action: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO public.audit_log (user_id, case_id, action, payload) VALUES (%s, %s, %s, %s)",
        (user_id, case_id, action, Jsonb(payload)),
    )


def model_label(router: ModelRouter) -> str:
    # The models that actually answered (after any quota fallback), else the configured ones.
    used = router.last_model
    plan = used.get("plan") or router.model_for("plan")
    synth = used.get("synthesize") or router.model_for("synthesize")
    models = plan if plan == synth else f"{plan}+{synth}"
    return f"{router.settings.llm_provider}:{models}"


def ask(
    app_conn: Conn,
    readonly_conn: Conn,
    case_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    router: ModelRouter | None = None,
    backends: ToolBackends | None = None,
    config: AgentConfig | None = None,
) -> AgentResult:
    """Run the investigator agent once and store the run. Raises CaseAccessError when RLS
    does not let `user_id` run on `case_id`."""
    router = router or ModelRouter()
    backends = backends or default_backends()
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")
    start = time.monotonic()
    model = model_label(router)

    try:
        with app_conn.transaction():
            row = app_conn.execute(
                "INSERT INTO public.agent_runs (case_id, user_id, question, status, model) "
                "VALUES (%s, %s, %s, 'running', %s) RETURNING id",
                (case_id, user_id, question, model),
            ).fetchone()
            if row is None:
                raise CaseAccessError("agent run was not created")
            run_id: uuid.UUID = row[0]
            _audit(
                app_conn,
                user_id,
                case_id,
                "agent.ask",
                {"run_id": str(run_id), "question": _clip_strings(question, 1000)},
            )
    except psycopg.errors.InsufficientPrivilege as exc:
        raise CaseAccessError("not a member of this case") from exc

    calls: list[dict[str, Any]] = []

    def on_tool_call(record: ToolCallRecord) -> None:
        stored = record.as_json()
        stored["input"] = _clip_strings(stored["input"])
        calls.append(stored)
        with app_conn.transaction():
            app_conn.execute(
                "UPDATE public.agent_runs SET tool_calls = %s WHERE id = %s",
                (Jsonb(calls), run_id),
            )
            _audit(
                app_conn,
                user_id,
                case_id,
                "agent.tool_call",
                {
                    "run_id": str(run_id),
                    "step": record.step,
                    "tool": record.tool,
                    "input": stored["input"],
                    "ok": record.ok,
                    "error": record.error,
                    "result_count": record.result_count,
                    "source_count": len(record.sources),
                    "sql": record.sql,
                    "conflict_id": record.conflict_id,
                },
            )

    # The shared demo user never stores conflicts (openapi /ask): flag_conflict returns a handle
    # only and the answer's conflict_record_id stays null. Read from the DB, not the caller.
    with app_conn.transaction():
        demo_row = app_conn.execute(
            "SELECT is_demo FROM public.users WHERE id = %s", (user_id,)
        ).fetchone()
    is_demo = demo_row is None or bool(demo_row[0])
    ctx = ToolContext(
        app_conn, readonly_conn, case_id, agent_run_id=run_id, persist_conflicts=not is_demo
    )
    loop = AgentLoop(router, ToolRuntime(ctx, backends), config, on_tool_call=on_tool_call)
    try:
        result: LoopResult = loop.run(question)
    except BaseException:
        latency = int((time.monotonic() - start) * 1000)
        try:
            with app_conn.transaction():
                app_conn.execute(
                    "UPDATE public.agent_runs SET status = 'failed', latency_ms = %s WHERE id = %s",
                    (latency, run_id),
                )
        except psycopg.Error:
            pass  # the original error is the one worth raising
        raise

    latency = int((time.monotonic() - start) * 1000)
    status = "failed" if result.model_failed else "succeeded"
    usage = result.usage.summary()
    plan = {
        "thoughts": [_clip_strings(t, 1000) for t in result.thoughts],
        "stop_reason": result.stop_reason,
        "validation_errors": result.validation_errors,
        "fell_back": result.fell_back,
        "prefetched_steps": result.prefetched_steps,
        "quotes_dropped": result.quotes_dropped,
        "usage": usage,
    }
    estimate = result.usage.list_price_usd()
    cost = 0.0 if router.settings.llm_free_tier else estimate
    model = model_label(router)
    with app_conn.transaction():
        app_conn.execute(
            "UPDATE public.agent_runs SET status = %s, model = %s, plan = %s, tool_calls = %s, "
            "answer = %s, citations = %s, latency_ms = %s, input_tokens = %s, "
            "output_tokens = %s, cost_usd = %s WHERE id = %s",
            (
                status,
                model,
                Jsonb(plan),
                Jsonb(calls),
                Jsonb(result.answer),
                Jsonb(result.answer["citations"]),
                latency,
                result.usage.input_tokens,
                result.usage.output_tokens,
                cost,
                run_id,
            ),
        )
    return AgentResult(
        run_id=run_id,
        status=status,
        answer=result.answer,
        tool_calls=result.tool_calls,
        thoughts=result.thoughts,
        latency_ms=latency,
        model=model,
        stop_reason=result.stop_reason,
        fell_back=result.fell_back,
        validation_errors=result.validation_errors,
        usage=usage,
    )
