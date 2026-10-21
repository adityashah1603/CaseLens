"""Executors for the six contract tools.

`get_entity`, `get_timeline` and `flag_conflict` are implemented here as parameterized SQL on a
`caselens_app` connection (RLS applies; `case_id` is bound by the runtime and also filtered on
explicitly). `search_evidence`, `search_video` and `query_records` delegate to injected backends
(`ToolBackends`); `default_backends()` wires the real `caselens_retrieval` /
`caselens_semantic_layer` functions with lazy imports, so this package never imports them at
module load and tests can use fakes.

Every call validates the model's input against the contract `input_schema` before running, and
the result against the contract `output_schema` before the model sees it.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

import psycopg
from jsonschema import Draft202012Validator
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from caselens_agent.contracts import load_tool_definitions
from caselens_agent.sources import SourceLedger, SourceRef, observe

Conn = psycopg.Connection[Any]
ToolBackend = Callable[[Conn, uuid.UUID, Mapping[str, Any]], Any]

ENTITY_MATCH_LIMIT = 5
# Per matched entity: the most confident, then newest, mentions. Enough to cite and to see
# which files talk about the entity, without flooding the context window.
MENTION_LIMIT = 20
TIMELINE_EVENT_LIMIT = 50
TRIGRAM_THRESHOLD = 0.3


class ToolError(Exception):
    """A tool call that failed in a way the model can be told about (safe message)."""


@dataclass
class ToolBackends:
    """Backends for the delegated tools. Each takes (conn, case_id, validated tool input)."""

    search_evidence: ToolBackend
    search_video: ToolBackend
    query_records: ToolBackend


def default_backends() -> ToolBackends:
    """The real retrieval / semantic-layer backends, imported lazily."""

    def search_evidence(conn: Conn, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
        import caselens_retrieval as r

        filters = args.get("filters")
        return r.search_evidence(
            conn,
            case_id,
            args["query"],
            filters=r.EvidenceFilters.model_validate(filters) if filters else None,
            top_k=int(args.get("top_k", 8)),
        )

    def search_video(conn: Conn, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
        import caselens_retrieval as r

        def ts(key: str) -> datetime | None:
            value = args.get(key)
            return None if value is None else datetime.fromisoformat(str(value))

        ids = args.get("evidence_ids")
        return r.search_video(
            conn,
            case_id,
            args["query"],
            time_from=ts("time_from"),
            time_to=ts("time_to"),
            evidence_ids=[uuid.UUID(str(i)) for i in ids] if ids else None,
            top_k=int(args.get("top_k", 8)),
        )

    def query_records(conn: Conn, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
        import caselens_semantic_layer as sl

        try:
            return sl.run_operation(conn, case_id, args["operation"], args["params"])
        except sl.OperationError as exc:
            raise ToolError(str(exc)) from exc

    return ToolBackends(search_evidence, search_video, query_records)


@dataclass
class ToolContext:
    """Runtime-bound context: connections (already on their roles), case, run."""

    app_conn: Conn
    readonly_conn: Conn
    case_id: uuid.UUID
    agent_run_id: uuid.UUID | None = None
    # False for the shared demo user (users.is_demo): flag_conflict checks the claims and
    # returns a handle but writes nothing, so visitors never write into the shared demo case.
    persist_conflicts: bool = True


@dataclass
class ToolOutcome:
    tool: str
    ok: bool
    observation: dict[str, Any] | None = None
    error: str | None = None
    output: dict[str, Any] | None = None
    handles: list[str] = field(default_factory=list)
    result_count: int = 0
    duration_ms: int = 0


# ---- contract validation ------------------------------------------------------------------


@cache
def _validators() -> dict[str, tuple[Draft202012Validator, Draft202012Validator]]:
    fc = Draft202012Validator.FORMAT_CHECKER
    return {
        name: (
            Draft202012Validator(tool["input_schema"], format_checker=fc),
            Draft202012Validator(tool["output_schema"], format_checker=fc),
        )
        for name, tool in load_tool_definitions().items()
    }


_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _describe(err: Any) -> str:
    """A schema error without the offending value: jsonschema messages quote the instance,
    which may be model-written text copied from evidence. Only schema-side facts are named."""
    kind = str(err.validator)
    expected = err.validator_value
    if kind == "required" and isinstance(expected, list) and isinstance(err.instance, dict):
        missing = [str(k) for k in expected if k not in err.instance]
        return "missing required properties: " + ", ".join(missing)
    if kind == "additionalProperties" and isinstance(err.schema, dict):
        allowed = sorted(str(k) for k in err.schema.get("properties", {}))
        return "unexpected properties; allowed: " + ", ".join(allowed)
    if kind in (
        "type",
        "format",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "const",
        "uniqueItems",
    ):
        shown = json.dumps(expected, ensure_ascii=True)[:80]
        return f"value fails {kind} {shown}"
    if kind == "enum" and isinstance(expected, list):
        return "value must be one of " + json.dumps(expected, ensure_ascii=True)[:200]
    return f"value fails the {kind} constraint"


def _schema_errors(validator: Draft202012Validator, value: Any, limit: int = 5) -> list[str]:
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    out = []
    for err in errors[:limit]:
        parts = [str(p) for p in err.absolute_path]
        # Path parts are property names or indexes; anything else is model-written: hide it.
        path = "/".join(p if p.isdigit() or _SAFE_KEY.match(p) else "?" for p in parts)
        out.append(f"{path or '(root)'}: {_describe(err)}")
    return out


def input_errors(tool: str, args: Any) -> list[str]:
    return _schema_errors(_validators()[tool][0], args)


def output_errors(tool: str, output: Any) -> list[str]:
    return _schema_errors(_validators()[tool][1], output)


# ---- get_entity ---------------------------------------------------------------------------

_NON_DIGITS = re.compile(r"\D")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_NAME_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


def lookup_keys(name: str) -> list[str]:
    """Candidate `entities.normalized_key` values for a name, plate or phone (same rules as
    ingest resolution)."""
    keys: list[str] = []
    text = name.casefold().replace("&", " and ")
    text = _LEADING_ARTICLE.sub("", _SPACES.sub(" ", _NAME_PUNCT.sub(" ", text)).strip())
    if text:
        keys += [text, f"desc:{text}"]
    digits = _NON_DIGITS.sub("", name)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        keys.append("+" + digits)
    plate = _NON_ALNUM.sub("", name.upper())
    if 2 <= len(plate) <= 8:
        keys.append(plate)
    return list(dict.fromkeys(keys))


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class GetEntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = None


_ENTITY_BY_ID = """
SELECT e.id, e.type, e.canonical_name, e.attributes
  FROM public.entities AS e
 WHERE e.case_id = %(case_id)s AND e.id = %(entity_id)s
   AND (%(type)s::text IS NULL OR e.type = %(type)s::text)
"""

# Score: exact normalized key 1.0, exact alias 0.95, else the best trigram similarity of the
# canonical name or any alias; substring matches of the name or an alias count at least 0.5.
_ENTITY_BY_NAME = """
SELECT e.id, e.type, e.canonical_name, e.attributes, s.score
  FROM public.entities AS e
 CROSS JOIN LATERAL (
     SELECT GREATEST(
         CASE WHEN e.normalized_key = ANY(%(keys)s::text[]) THEN 1.0 ELSE 0 END,
         CASE WHEN lower(e.canonical_name) = lower(%(name)s) THEN 1.0 ELSE 0 END,
         CASE WHEN e.canonical_name ILIKE %(pattern)s THEN 0.5 ELSE 0 END,
         similarity(e.canonical_name, %(name)s),
         COALESCE((
             SELECT max(GREATEST(
                 CASE WHEN lower(a.alias) = lower(%(name)s) THEN 0.95 ELSE 0 END,
                 CASE WHEN a.alias ILIKE %(pattern)s THEN 0.5 ELSE 0 END,
                 similarity(a.alias, %(name)s)))
               FROM jsonb_array_elements_text(
                   CASE WHEN jsonb_typeof(e.attributes -> 'aliases') = 'array'
                        THEN e.attributes -> 'aliases' ELSE '[]'::jsonb END) AS a(alias)
         ), 0)
     )::float8 AS score
 ) AS s
 WHERE e.case_id = %(case_id)s
   AND (%(type)s::text IS NULL OR e.type = %(type)s::text)
   AND s.score >= %(threshold)s
 ORDER BY s.score DESC, e.canonical_name, e.id
 LIMIT %(limit)s
"""

_LINKED = """
SELECT DISTINCT CASE WHEN l.from_entity_id = %(entity_id)s THEN l.to_entity_id
                     ELSE l.from_entity_id END AS other
  FROM public.entity_links AS l
 WHERE l.case_id = %(case_id)s AND l.status = 'active'
   AND (l.from_entity_id = %(entity_id)s OR l.to_entity_id = %(entity_id)s)
 ORDER BY 1
"""

_MENTIONS = """
SELECT m.id, m.chunk_id, c.evidence_id, m.surface_text, m.confidence, c.locator
  FROM public.mentions AS m
  JOIN public.chunks AS c ON c.id = m.chunk_id AND c.case_id = m.case_id
 WHERE m.case_id = %(case_id)s AND m.entity_id = %(entity_id)s
 ORDER BY m.confidence DESC NULLS LAST, m.created_at DESC, m.id
 LIMIT %(limit)s
"""


def _aliases(attributes: dict[str, Any]) -> list[str]:
    raw = attributes.get("aliases")
    return [str(a) for a in raw] if isinstance(raw, list) else []


def get_entity(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    inp = GetEntityInput.model_validate(args)
    conn = ctx.app_conn
    base = {"case_id": ctx.case_id, "type": inp.type}
    if inp.entity_id is not None:
        rows = conn.execute(_ENTITY_BY_ID, {**base, "entity_id": inp.entity_id}).fetchall()
    else:
        name = (inp.name or "").strip()
        rows = conn.execute(
            _ENTITY_BY_NAME,
            {
                **base,
                "name": name,
                "keys": lookup_keys(name),
                "pattern": _like_pattern(name),
                "threshold": TRIGRAM_THRESHOLD,
                "limit": ENTITY_MATCH_LIMIT,
            },
        ).fetchall()
    matches = []
    for row in rows:
        entity_id, etype, canonical, attributes = row[0], row[1], row[2], row[3]
        params = {"case_id": ctx.case_id, "entity_id": entity_id}
        linked = [str(r[0]) for r in conn.execute(_LINKED, params).fetchall()]
        mentions = [
            {
                "mention_id": str(m[0]),
                "chunk_id": str(m[1]),
                "evidence_id": str(m[2]),
                "surface_text": m[3],
                "confidence": float(m[4]) if m[4] is not None else 0.0,
                "locator": m[5],
            }
            for m in conn.execute(_MENTIONS, {**params, "limit": MENTION_LIMIT}).fetchall()
        ]
        attrs = dict(attributes or {})
        matches.append(
            {
                "entity_id": str(entity_id),
                "type": etype,
                "canonical_name": canonical,
                "attributes": attrs,
                "aliases": _aliases(attrs),
                "linked_entity_ids": linked,
                "mentions": mentions,
            }
        )
    return {"matches": matches}


# ---- get_timeline -------------------------------------------------------------------------


class GetTimelineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: uuid.UUID | None = None
    time_from: AwareDatetime | None = None
    time_to: AwareDatetime | None = None


# The entity set is the entity plus its actively linked entities (one hop, either direction).
# An event involves the set when its location is in it, or a source chunk mentions it.
_EVENTS = """
WITH ents AS (
    SELECT %(entity_id)s::uuid AS id WHERE %(entity_id)s::uuid IS NOT NULL
    UNION
    SELECT CASE WHEN l.from_entity_id = %(entity_id)s::uuid THEN l.to_entity_id
                ELSE l.from_entity_id END
      FROM public.entity_links AS l
     WHERE l.case_id = %(case_id)s AND l.status = 'active'
       AND (l.from_entity_id = %(entity_id)s::uuid OR l.to_entity_id = %(entity_id)s::uuid)
)
SELECT ev.id, ev.t_start, ev.t_end, ev.location_id, loc.canonical_name, ev.description,
       ev.confidence, ev.source_chunk_ids
  FROM public.events AS ev
  LEFT JOIN public.entities AS loc ON loc.id = ev.location_id AND loc.case_id = ev.case_id
 WHERE ev.case_id = %(case_id)s
   AND (%(time_from)s::timestamptz IS NULL
        OR COALESCE(ev.t_end, ev.t_start) >= %(time_from)s::timestamptz)
   AND (%(time_to)s::timestamptz IS NULL OR ev.t_start <= %(time_to)s::timestamptz)
   AND (%(entity_id)s::uuid IS NULL
        OR ev.location_id IN (SELECT id FROM ents)
        OR EXISTS (SELECT 1 FROM public.mentions AS m
                    WHERE m.case_id = ev.case_id
                      AND m.chunk_id = ANY(ev.source_chunk_ids)
                      AND m.entity_id IN (SELECT id FROM ents)))
 ORDER BY ev.t_start, ev.id
 LIMIT %(limit)s
"""

_EVENT_SOURCES = """
SELECT c.id, c.evidence_id, c.locator
  FROM public.chunks AS c
 WHERE c.case_id = %(case_id)s AND c.id = ANY(%(ids)s::uuid[])
"""


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def get_timeline(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    inp = GetTimelineInput.model_validate(args)
    conn = ctx.app_conn
    rows = conn.execute(
        _EVENTS,
        {
            "case_id": ctx.case_id,
            "entity_id": inp.entity_id,
            "time_from": inp.time_from,
            "time_to": inp.time_to,
            "limit": TIMELINE_EVENT_LIMIT,
        },
    ).fetchall()
    chunk_ids = sorted({cid for r in rows for cid in (r[7] or [])}, key=str)
    chunks: dict[Any, tuple[str, dict[str, Any]]] = {}
    if chunk_ids:
        for c in conn.execute(
            _EVENT_SOURCES, {"case_id": ctx.case_id, "ids": chunk_ids}
        ).fetchall():
            chunks[c[0]] = (str(c[1]), c[2])
    events = []
    for r in rows:
        sources = [
            {"chunk_id": str(cid), "evidence_id": chunks[cid][0], "locator": chunks[cid][1]}
            for cid in (r[7] or [])
            if cid in chunks
        ]
        events.append(
            {
                "event_id": str(r[0]),
                "t_start": _iso(r[1]),
                "t_end": _iso(r[2]),
                "location": None if r[3] is None else {"entity_id": str(r[3]), "name": r[4]},
                "description": r[5],
                "confidence": float(r[6]) if r[6] is not None else 0.0,
                "sources": sources,
            }
        )
    return {"events": events}


# ---- flag_conflict ------------------------------------------------------------------------

_INSERT_CONFLICT = """
INSERT INTO public.conflicts (case_id, agent_run_id, description, claim_a, claim_b)
VALUES (%(case_id)s, %(run_id)s, %(description)s, %(claim_a)s, %(claim_b)s)
RETURNING id, status
"""


def expand_conflict_refs(args: Any, ledger: SourceLedger) -> Any:
    """Replace source handles (e.g. "S3") in flag_conflict claims with the full SourceRef the
    tool returned, so the model never types ids or locators. Unknown handles raise ToolError;
    other values are left for schema validation."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    for side in ("claim_a", "claim_b"):
        claim = out.get(side)
        if not isinstance(claim, dict) or not isinstance(claim.get("source_refs"), list):
            continue
        refs: list[Any] = []
        for item in claim["source_refs"]:
            if isinstance(item, str):
                observed = ledger.get(item)
                if observed is None:
                    raise ToolError(f"{side}: unknown source handle {item!r}")
                refs.append(observed.ref.contract_ref())
            else:
                refs.append(item)
        out[side] = {**claim, "source_refs": refs}
    return out


def flag_conflict(
    ctx: ToolContext, args: Mapping[str, Any], ledger: SourceLedger
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    for side in ("claim_a", "claim_b"):
        for ref in args[side]["source_refs"]:
            try:
                parsed = SourceRef.from_contract_ref(ref)
            except ValidationError as exc:
                raise ToolError(f"{side}: invalid source ref") from exc
            observed = ledger.find(parsed)
            if observed is None or observed.ref != parsed:
                raise ToolError(f"{side}: source was not returned by a tool in this run")
    if not ctx.persist_conflicts:
        # Demo user: nothing is stored; the handle only ties the answer's conflict to this call
        # (its conflict_record_id stays null).
        return {"conflict_id": str(uuid.uuid4()), "status": "open"}
    row = ctx.app_conn.execute(
        _INSERT_CONFLICT,
        {
            "case_id": ctx.case_id,
            "run_id": ctx.agent_run_id,
            "description": args["description"],
            "claim_a": Jsonb(args["claim_a"]),
            "claim_b": Jsonb(args["claim_b"]),
        },
    ).fetchone()
    if row is None:
        raise ToolError("conflict was not stored")
    return {"conflict_id": str(row[0]), "status": row[1]}


# ---- dispatcher ---------------------------------------------------------------------------


def _as_dict(output: Any) -> dict[str, Any]:
    dumped = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    if not isinstance(dumped, dict):
        raise ToolError("tool returned an unexpected result")
    return dumped


class ToolRuntime:
    """Runs contract tools for one agent run and records what they returned."""

    def __init__(
        self, ctx: ToolContext, backends: ToolBackends, ledger: SourceLedger | None = None
    ) -> None:
        self.ctx = ctx
        self.backends = backends
        self.ledger = ledger or SourceLedger()
        self.definitions = load_tool_definitions()

    def case_year(self) -> int | None:
        """The year of the case's latest timed chunk (dates in questions rarely give one)."""
        try:
            with self.ctx.app_conn.transaction():
                row = self.ctx.app_conn.execute(
                    "SELECT max(t_start) FROM public.chunks WHERE case_id = %s", (self.ctx.case_id,)
                ).fetchone()
        except psycopg.Error:
            return None
        value = row[0] if row else None
        return value.year if isinstance(value, datetime) else None

    def _dispatch(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self.ctx
        if tool == "get_entity":
            with ctx.app_conn.transaction():
                return get_entity(ctx, args)
        if tool == "get_timeline":
            with ctx.app_conn.transaction():
                return get_timeline(ctx, args)
        if tool == "flag_conflict":
            with ctx.app_conn.transaction():
                return flag_conflict(ctx, args, self.ledger)
        if tool == "search_evidence":
            with ctx.app_conn.transaction():
                return _as_dict(self.backends.search_evidence(ctx.app_conn, ctx.case_id, args))
        if tool == "search_video":
            with ctx.app_conn.transaction():
                return _as_dict(self.backends.search_video(ctx.app_conn, ctx.case_id, args))
        if tool == "query_records":
            return _as_dict(self.backends.query_records(ctx.readonly_conn, ctx.case_id, args))
        raise ToolError(f"unknown tool {tool!r}")

    def call(self, tool: str, raw_args: Any) -> ToolOutcome:
        start = time.monotonic()
        outcome = ToolOutcome(tool=tool, ok=False)
        try:
            if tool not in self.definitions:
                raise ToolError(f"unknown tool {tool!r}; use one of: {', '.join(self.definitions)}")
            args = raw_args
            if tool == "flag_conflict":
                args = expand_conflict_refs(raw_args, self.ledger)
            errors = input_errors(tool, args)
            if errors:
                raise ToolError("input does not match the tool schema: " + "; ".join(errors))
            output = self._dispatch(tool, args)
            errors = output_errors(tool, output)
            if errors:
                raise ToolError("tool output failed contract validation")
            observation, handles, count = observe(tool, output, self.ledger)
            if tool == "flag_conflict":
                observation["conflict"] = self.ledger.add_conflict(
                    output["conflict_id"],
                    str(args["description"]),
                    stored=self.ctx.persist_conflicts,
                )
                if not self.ctx.persist_conflicts:
                    observation["stored"] = False
            outcome.ok = True
            outcome.output = output
            outcome.observation = observation
            outcome.handles = handles
            outcome.result_count = count
        except ToolError as exc:
            outcome.error = str(exc)
        except ValidationError as exc:
            details = exc.errors()
            if details:
                loc = "/".join(str(p) for p in details[0]["loc"])
                outcome.error = f"invalid input: {loc}: {details[0]['msg']}"
            else:
                outcome.error = "invalid input"
        except ValueError as exc:  # backend argument errors (safe messages by convention)
            outcome.error = f"invalid input: {str(exc)[:300]}"
        except psycopg.Error as exc:
            # Never leak SQL text or values; the class name is enough for the model to move on.
            outcome.error = f"database error ({type(exc).__name__})"
        outcome.duration_ms = int((time.monotonic() - start) * 1000)
        return outcome
