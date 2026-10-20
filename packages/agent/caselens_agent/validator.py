"""The answer validator: the answer JSON Schema plus the runtime rules it cannot express.

Checks, in order (see `answer.schema.json` description and SYSTEM_DESIGN 7d):

1. The answer validates against `answer.schema.json` (draft 2020-12, formats checked).
2. `answered` needs at least one claim and every claim cites at least one citation (schema);
   `insufficient_evidence` carries no claims.
3. Every citation id in claims and conflicts resolves to `citations[]`.
4. Provenance: every citation matches a source a tool returned in this run, with the same
   chunk / record id, evidence id and locator.
5. A non-null `quote` is a whitespace-normalized substring of text the tool returned for it.
6. Conflicts: `conflict_record_id` must be an id `flag_conflict` returned in this run, and an
   `answered` answer surfaces every conflict flagged in the run (never silently resolved).
7. Ids are unique within their arrays.

Returns a list of human-readable errors; an empty list means the answer may be shown.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from caselens_agent.contracts import load_answer_schema
from caselens_agent.sources import SourceLedger, SourceRef, normalize_ws


@cache
def _answer_validator() -> Draft202012Validator:
    return Draft202012Validator(
        load_answer_schema(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def schema_errors(answer: Any, limit: int = 10) -> list[str]:
    errors = sorted(_answer_validator().iter_errors(answer), key=lambda e: list(e.absolute_path))
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message[:200]}"
        for e in errors[:limit]
    ]


def _duplicates(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for item in items:
        ident = item.get("id") if isinstance(item, dict) else None
        if isinstance(ident, str):
            if ident in seen and ident not in dupes:
                dupes.append(ident)
            seen.add(ident)
    return dupes


def _citation_ref(citation: dict[str, Any]) -> SourceRef:
    if citation["source_type"] == "chunk":
        return SourceRef.chunk(
            str(citation["chunk_id"]), str(citation["evidence_id"]), citation["locator"]
        )
    return SourceRef(
        source_type="record",
        evidence_id=str(citation["evidence_id"]),
        table=citation.get("table"),
        record_id=citation.get("record_id"),
        locator=citation["locator"],
    )


def validate_answer(answer: Any, ledger: SourceLedger) -> list[str]:
    """Validate a contract answer against the schema and the sources observed in this run."""
    errors = schema_errors(answer)
    if errors:
        return errors
    claims: list[dict[str, Any]] = answer["claims"]
    citations: list[dict[str, Any]] = answer["citations"]
    conflicts: list[dict[str, Any]] = answer["conflicts"]

    for name, items in (("claims", claims), ("citations", citations), ("conflicts", conflicts)):
        for dup in _duplicates(items):
            errors.append(f"{name}: duplicate id {dup}")

    if answer["status"] == "insufficient_evidence" and claims:
        errors.append("an insufficient_evidence answer must not contain claims")

    by_id = {c["id"]: c for c in citations}
    for claim in claims:
        if not claim["citation_ids"]:
            errors.append(f"claim {claim['id']} has no citation")
        for cid in claim["citation_ids"]:
            if cid not in by_id:
                errors.append(f"claim {claim['id']} cites unknown citation {cid}")
    for conflict in conflicts:
        for side in ("claim_a", "claim_b"):
            for cid in conflict[side]["citation_ids"]:
                if cid not in by_id:
                    errors.append(f"conflict {conflict['id']} {side} cites unknown citation {cid}")

    for citation in citations:
        cid = citation["id"]
        if citation["source_type"] == "chunk" and (
            citation.get("table") is not None or citation.get("record_id") is not None
        ):
            errors.append(f"citation {cid}: a chunk citation has no table / record_id")
            continue
        if citation["source_type"] == "record" and citation.get("chunk_id") is not None:
            errors.append(f"citation {cid}: a record citation has no chunk_id")
            continue
        ref = _citation_ref(citation)
        observed = ledger.find(ref)
        if observed is None:
            errors.append(f"citation {cid}: source was not returned by any tool in this run")
            continue
        if observed.ref != ref:
            errors.append(f"citation {cid}: evidence_id or locator differs from the tool result")
            continue
        quote = citation.get("quote")
        if quote:
            needle = normalize_ws(quote)
            if not any(needle in normalize_ws(t) for t in observed.texts):
                errors.append(f"citation {cid}: quote is not an exact excerpt of the source text")

    flagged = ledger.flagged_ids()
    surfaced: set[str] = set()
    for conflict in conflicts:
        record_id = conflict.get("conflict_record_id")
        if record_id is not None:
            if record_id not in flagged:
                errors.append(
                    f"conflict {conflict['id']}: conflict_record_id was not returned by "
                    "flag_conflict in this run"
                )
            surfaced.add(record_id)
    if answer["status"] == "answered":
        for f in ledger.flagged.values():
            if f.conflict_id not in surfaced:
                errors.append(
                    f"conflict {f.handle} ({f.description[:80]}) was flagged in this run but "
                    "is not shown in conflicts[]"
                )
    return errors


def insufficient_evidence(
    reason: str, ledger: SourceLedger | None = None, extra: list[str] | None = None
) -> dict[str, Any]:
    """The fallback answer: no claims, an explanation in open_questions."""
    questions = [reason, *(extra or [])]
    if ledger is not None:
        for f in ledger.flagged.values():
            questions.append(
                f"A conflict was flagged for human review (record {f.conflict_id}): "
                f"{f.description[:300]}"
            )
    return {
        "status": "insufficient_evidence",
        "answer": "Insufficient evidence: no answer could be given in which every claim is "
        "backed by a source returned in this run.",
        "claims": [],
        "citations": [],
        "conflicts": [],
        "confidence": 0.0,
        "open_questions": [q for q in questions if q.strip()],
    }
