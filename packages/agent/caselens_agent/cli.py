"""`python -m caselens_agent ask --case-id <uuid> --user-id <uuid> "question"`.

Connects with CASELENS_DATABASE_URL twice: one connection as `caselens_app` (runs, audit,
tools) and one as `caselens_readonly` (semantic-layer SQL), both with `app.user_id` set so RLS
applies. Prints the answer, claims with their citations, conflicts and the tool trace.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from caselens_agent.runner import AgentResult, CaseAccessError, ask
from caselens_agent.tools import Conn


def _libpq(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}" if sep else url


def connect_as(url: str, role: str, user_id: uuid.UUID) -> Conn:
    conn = psycopg.connect(_libpq(url), autocommit=True)
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user_id),))
    return conn


def _locator(loc: dict[str, Any]) -> str:
    kind = loc.get("kind")
    if kind == "pdf":
        return f"page {loc.get('page')}" + (" (bbox)" if loc.get("bbox") else "")
    if kind == "audio":
        return f"{loc.get('start_ms')}-{loc.get('end_ms')} ms" + (
            f" {loc['speaker']}" if loc.get("speaker") else ""
        )
    if kind == "video":
        frame = f" frame {loc['frame_idx']}" if loc.get("frame_idx") is not None else ""
        return f"{loc.get('start_ms')} ms{frame}" + (
            f" track {loc['track_id']}" if loc.get("track_id") else ""
        )
    if kind == "record":
        return f"{loc.get('table')} row {loc.get('row_id')}"
    return str(loc)


def format_result(result: AgentResult, filenames: dict[str, str]) -> str:
    a = result.answer
    cites = {c["id"]: c for c in a["citations"]}

    def cite_line(cid: str) -> str:
        c = cites[cid]
        name = filenames.get(c["evidence_id"], c["evidence_id"])
        quote = f' "{c["quote"]}"' if c.get("quote") else ""
        return f"      [{cid}] {name} @ {_locator(c['locator'])}{quote}"

    lines = [
        f"run {result.run_id}  status={result.status}  answer={a['status']}  "
        f"confidence={a['confidence']}  latency={result.latency_ms} ms  model={result.model}",
        "",
        a["answer"],
        "",
    ]
    for claim in a["claims"]:
        when = f" [{claim['t_start']}]" if claim.get("t_start") else ""
        lines.append(f"  {claim['id']}{when}: {claim['text']}")
        lines.extend(cite_line(cid) for cid in claim["citation_ids"])
    for cf in a["conflicts"]:
        lines.append(f"  CONFLICT {cf['id']}: {cf['description']}")
        for side in ("claim_a", "claim_b"):
            lines.append(f"    {side}: {cf[side]['text']}")
            lines.extend(cite_line(cid) for cid in cf[side]["citation_ids"])
        if cf.get("conflict_record_id"):
            lines.append(f"    stored as conflict {cf['conflict_record_id']}")
    if a["open_questions"]:
        lines.append("  Open questions:")
        lines.extend(f"    - {q}" for q in a["open_questions"])
    lines.append("")
    lines.append(f"Tool trace ({len(result.tool_calls)} calls, stop: {result.stop_reason}):")
    for tc in result.tool_calls:
        status = f"{tc.result_count} results" if tc.ok else f"ERROR {tc.error}"
        lines.append(f"  {tc.step}. {tc.tool} {tc.input} -> {status} {tc.duration_ms} ms")
    if result.fell_back:
        lines.append("Validator rejected the draft answers:")
        for i, errs in enumerate(result.validation_errors, start=1):
            lines.extend(f"  attempt {i}: {e}" for e in errs[:10])
    return "\n".join(lines)


def _filenames(conn: Conn, answer: dict[str, Any]) -> dict[str, str]:
    ids = sorted({c["evidence_id"] for c in answer["citations"]})
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, original_filename FROM public.evidence WHERE id = ANY(%s::uuid[])",
        (ids,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m caselens_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ask = sub.add_parser("ask", help="ask the investigator agent a question about a case")
    p_ask.add_argument("--case-id", type=uuid.UUID, required=True)
    p_ask.add_argument("--user-id", type=uuid.UUID, required=True)
    p_ask.add_argument("question")
    args = parser.parse_args(argv)

    url = os.environ.get("CASELENS_DATABASE_URL")
    if not url:
        print("CASELENS_DATABASE_URL is not set", file=sys.stderr)
        return 2
    with (
        connect_as(url, "caselens_app", args.user_id) as app_conn,
        connect_as(url, "caselens_readonly", args.user_id) as ro_conn,
    ):
        try:
            result = ask(app_conn, ro_conn, args.case_id, args.user_id, args.question)
        except CaseAccessError as exc:
            print(f"access denied: {exc}", file=sys.stderr)
            return 3
        print(format_result(result, _filenames(app_conn, result.answer)))
    return 0
