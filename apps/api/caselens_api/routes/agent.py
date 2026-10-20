"""Investigator agent: ask (synchronous run), run list and run trace."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Query

from caselens_api import models as m
from caselens_api.common import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    cursor_params,
    decode_cursor,
    encode_cursor,
    parse_id,
)
from caselens_api.db import Conn
from caselens_api.deps import ViewerCase
from caselens_api.problems import ProblemError, not_found, validation

router = APIRouter(prefix="/v1/cases/{case_id}")

Cursor = Annotated[str | None, Query(max_length=512)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
TOOL_NAMES = frozenset(get_args(m.ToolName))

_RUN_SELECT = """
SELECT r.id, r.case_id, r.user_id, r.question, r.status, r.model, r.plan, r.tool_calls,
       r.answer, r.latency_ms, r.input_tokens, r.output_tokens, r.cost_usd, r.created_at
  FROM public.agent_runs AS r
 WHERE r.case_id = %(case_id)s
"""
_RUN_ONE = _RUN_SELECT + " AND r.id = %(run_id)s"
_RUN_AFTER_FAILURE = (
    _RUN_SELECT
    + """
   AND r.user_id = %(user_id)s AND r.question = %(question)s AND r.created_at >= %(since)s
 ORDER BY r.created_at DESC
 LIMIT 1
"""
)
_RUN_PAGE = """
SELECT r.id, r.user_id, r.question, r.status, r.answer ->> 'status', r.model, r.latency_ms,
       r.cost_usd, r.created_at
  FROM public.agent_runs AS r
 WHERE r.case_id = %(case_id)s
   AND (%(c_ts)s::timestamptz IS NULL OR (r.created_at, r.id) < (%(c_ts)s, %(c_id)s::uuid))
 ORDER BY r.created_at DESC, r.id DESC
 LIMIT %(limit)s
"""


def _plan(raw: Any) -> list[str] | None:
    """agent_runs.plan holds {thoughts, stop_reason, ...}; the contract's plan is the steps."""
    if isinstance(raw, dict):
        raw = raw.get("thoughts")
    if isinstance(raw, list):
        return [str(step) for step in raw]
    return None


def _summary(call: dict[str, Any]) -> str:
    if not call.get("ok"):
        return f"error: {call.get('error') or 'tool call failed'}"
    count = int(call.get("result_count") or 0)
    sources = call.get("sources") or []
    text = f"{count} result{'s' if count != 1 else ''}, {len(sources)} source(s)"
    if call.get("conflict_id"):
        text += f"; stored conflict {call['conflict_id']}"
    return text


def tool_calls(raw: Any) -> list[m.ToolCall]:
    out = []
    for call in raw if isinstance(raw, list) else []:
        if not isinstance(call, dict) or call.get("tool") not in TOOL_NAMES:
            continue  # the contract's ToolCall.name only admits the six contract tools
        tool_input = call.get("input")
        out.append(
            m.ToolCall(
                name=call["tool"],
                input=tool_input if isinstance(tool_input, dict) else {},
                output_summary=_summary(call),
                sql=call.get("sql"),
                latency_ms=max(0, int(call.get("duration_ms") or 0)),
                error=call.get("error"),
            )
        )
    return out


def run_from_row(row: tuple[Any, ...]) -> m.AgentRun:
    return m.AgentRun(
        id=row[0],
        case_id=row[1],
        user_id=row[2],
        question=row[3],
        status=row[4],
        model=row[5],
        plan=_plan(row[6]),
        tool_calls=tool_calls(row[7]),
        answer=row[8] if isinstance(row[8], dict) else None,
        latency_ms=row[9],
        input_tokens=row[10],
        output_tokens=row[11],
        cost_usd=float(row[12]) if row[12] is not None else None,
        created_at=row[13],
    )


def load_run(conn: Conn, case_id: uuid.UUID, run_id: uuid.UUID) -> m.AgentRun:
    row = conn.execute(_RUN_ONE, {"case_id": case_id, "run_id": run_id}).fetchone()
    if row is None:
        raise not_found("No such run.")
    return run_from_row(row)


@router.post("/ask", response_model=m.AgentRun)
def ask_question(body: m.AskRequest, case: ViewerCase) -> m.AgentRun:
    from caselens_agent.runner import CaseAccessError, ask

    services = case.services
    question = body.question.strip()
    if not question:
        raise validation("question must not be blank.", ["body", "question"])
    wait = services.ask_limiter.check(case.user.id)
    if wait is not None:
        raise ProblemError(
            429,
            "Too many questions; wait before asking again.",
            headers={"Retry-After": str(wait)},
        )
    since = datetime.now(UTC)
    try:
        with services.require_db().session(case.user.id, readonly=True) as readonly_conn:
            result = ask(
                case.conn,
                readonly_conn,
                case.case_id,
                case.user.id,
                question,
                **services.ask_kwargs,
            )
    except CaseAccessError as exc:
        raise not_found("No such case.") from exc
    except Exception:
        # ask() marks the run failed before re-raising; return that run if it was created.
        row = case.conn.execute(
            _RUN_AFTER_FAILURE,
            {
                "case_id": case.case_id,
                "user_id": case.user.id,
                "question": question,
                "since": since,
            },
        ).fetchone()
        if row is None:
            raise
        return run_from_row(row)
    return load_run(case.conn, case.case_id, result.run_id)


@router.get("/runs", response_model=m.AgentRunList)
def list_runs(
    case: ViewerCase, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> m.AgentRunList:
    after = decode_cursor(cursor, (datetime, uuid.UUID))
    rows = case.conn.execute(
        _RUN_PAGE,
        {"case_id": case.case_id, **cursor_params(after, ("c_ts", "c_id")), "limit": limit + 1},
    ).fetchall()
    items = [
        m.AgentRunSummary(
            id=r[0],
            user_id=r[1],
            question=r[2],
            status=r[3],
            answer_status=r[4] if r[4] in ("answered", "insufficient_evidence") else None,
            model=r[5],
            latency_ms=r[6],
            cost_usd=float(r[7]) if r[7] is not None else None,
            created_at=r[8],
        )
        for r in rows[:limit]
    ]
    nxt = encode_cursor(rows[limit - 1][8], rows[limit - 1][0]) if len(rows) > limit else None
    return m.AgentRunList(items=items, next_cursor=nxt)


@router.get("/runs/{run_id}", response_model=m.AgentRun)
def get_run(run_id: str, case: ViewerCase) -> m.AgentRun:
    return load_run(case.conn, case.case_id, parse_id(run_id, "run"))
