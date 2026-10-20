"""Shared `extract` stage: entities, mentions, events and resolution for text-bearing chunks.

Runs after `embed` in the pdf, audio and video pipelines.

1. Deterministic pass (confidence 1.0): phone numbers -> E.164, license plates (synthetic
   `SYN1234` plates at 1.0, other plate-shaped tokens at GENERIC_PLATE_CONFIDENCE).
2. LLM pass through `ModelRouter.complete_json(task="extract")`, several chunks per call
   (MAX_BATCH_CHARS), each chunk in an `evidence_block`. With no model key configured the pass
   is skipped and the summary says so; any other model error fails the stage so it retries.
3. Event times are resolved to wall clock (`extraction.timeres`); events without a resolvable
   time are counted, not written (events.t_start is NOT NULL).
4. Writes are idempotent: this evidence's previous mentions, and events sourced only from its
   chunks, are deleted first. Entities are shared by the case and reused by normalized key.
5. Resolution passes 2 and 3 (fuzzy + LLM adjudication) link the entities this file touched.

Evidence text is data: it only reaches the model inside delimited blocks.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from caselens_workers.context import StageContext, StageResult
from caselens_workers.db import Conn
from caselens_workers.extraction.patterns import find_phones, find_plates, find_times
from caselens_workers.extraction.prompts import (
    ADJUDICATE_SYSTEM,
    EXTRACT_SYSTEM,
    EntityContext,
    PromptChunk,
    adjudicate_user_prompt,
    batch_chunks,
    extract_user_prompt,
)
from caselens_workers.extraction.schemas import (
    AdjudicationOutput,
    ExtractedEntity,
    ExtractedEvent,
    ExtractionOutput,
)
from caselens_workers.extraction.timeres import case_timezone, resolve_event_time
from caselens_workers.resolve import (
    Candidate,
    EntityType,
    Verdict,
    add_mention,
    ensure_entity,
    link_similar_entities,
    lock_case,
    merge_attributes,
    normalize_name,
    normalize_phone,
    normalize_plate,
)

if TYPE_CHECKING:
    from caselens_agent.router import ModelRouter

REVIEW_THRESHOLD = 0.6
ADJUDICATION_MIN_CONFIDENCE = 0.5
# A model-named surface text that is not in the cited chunk keeps half its confidence.
UNVERIFIED_SURFACE_FACTOR = 0.5
# Router errors that mean "no model is configured" (skip the LLM pass) rather than a failure.
_NOT_CONFIGURED = ("is not set", "no responder given")


@dataclass(frozen=True)
class ChunkRow:
    id: uuid.UUID
    index: int
    text: str
    t_start: datetime | None
    t_end: datetime | None


@dataclass
class _Writes:
    """Everything one run will write, collected before taking the case lock."""

    # (type, key) -> (canonical name, attributes)
    entities: dict[tuple[EntityType, str], tuple[str, dict[str, Any]]] = field(default_factory=dict)
    # (type, key, chunk id, casefolded surface) -> (surface, confidence)
    mentions: dict[tuple[EntityType, str, uuid.UUID, str], tuple[str, float]] = field(
        default_factory=dict
    )
    events: list[_EventOut] = field(default_factory=list)

    def entity(self, kind: EntityType, key: str, name: str, attrs: dict[str, Any]) -> None:
        if (kind, key) in self.entities:
            old_name, old_attrs = self.entities[(kind, key)]
            merged = {**attrs, **old_attrs}
            aliases = list(old_attrs.get("aliases") or [])
            aliases += [a for a in attrs.get("aliases") or [] if a not in aliases]
            if aliases:
                merged["aliases"] = aliases
            self.entities[(kind, key)] = (old_name, merged)
        else:
            self.entities[(kind, key)] = (name, attrs)

    def mention(
        self, kind: EntityType, key: str, chunk: uuid.UUID, surface: str, confidence: float
    ) -> None:
        slot = (kind, key, chunk, surface.casefold())
        if slot not in self.mentions or self.mentions[slot][1] < confidence:
            self.mentions[slot] = (surface, confidence)


@dataclass(frozen=True)
class _EventOut:
    description: str
    t_start: datetime
    t_end: datetime | None
    location: tuple[str, str] | None  # (name, key)
    chunk_ids: tuple[uuid.UUID, ...]
    confidence: float
    needs_review: bool


def _load_chunks(conn: Conn, evidence_id: uuid.UUID) -> list[ChunkRow]:
    rows = conn.execute(
        "SELECT id, chunk_index, coalesce(text, ''), t_start, t_end FROM public.chunks "
        "WHERE evidence_id = %s ORDER BY chunk_index",
        (evidence_id,),
    ).fetchall()
    return [ChunkRow(*r) for r in rows]


def _deterministic(chunks: Sequence[ChunkRow], out: _Writes) -> int:
    """Phones and plates into `out`; returns the number of clock times seen (for the summary)."""
    times = 0
    for chunk in chunks:
        for phone in find_phones(chunk.text):
            out.entity("phone", phone.value, phone.value, {})
            out.mention("phone", phone.value, chunk.id, phone.surface, phone.confidence)
        for plate in find_plates(chunk.text):
            out.entity("vehicle", plate.value, plate.value, {"plate": plate.value})
            out.mention("vehicle", plate.value, chunk.id, plate.surface, plate.confidence)
        times += len(find_times(chunk.text))
    return times


def entity_key(entity: ExtractedEntity) -> tuple[str, dict[str, Any]] | None:
    """Normalized key and attributes for a model-extracted entity (None: unusable)."""
    attrs: dict[str, Any] = {
        k: v for k, v in entity.attributes.model_dump().items() if v not in (None, "")
    }
    aliases = [a for a in entity.aliases if a.strip() and a != entity.name]
    if aliases:
        attrs["aliases"] = aliases
    if entity.type == "phone":
        phone = normalize_phone(entity.name) or normalize_phone(entity.surface_text)
        return (phone, {}) if phone else None
    if entity.type == "vehicle":
        candidates = [entity.attributes.plate or ""]
        candidates += [p.value for p in find_plates(f"{entity.name} {entity.surface_text}")]
        for raw in candidates:
            plate = normalize_plate(raw) if raw else None
            if plate is not None:
                attrs["plate"] = plate
                return plate, attrs
        name = normalize_name(entity.name)
        return (f"desc:{name}", attrs) if name else None
    name = normalize_name(entity.name)
    return (name, attrs) if name else None


def _locate(surface: str, ref: int, batch: dict[int, ChunkRow]) -> tuple[ChunkRow, float] | None:
    """The chunk a surface text is in, and a confidence factor (1.0 when verified)."""
    needle = surface.casefold()
    cited = batch.get(ref)
    if cited is not None and needle in cited.text.casefold():
        return cited, 1.0
    for chunk in batch.values():
        if needle in chunk.text.casefold():
            return chunk, 1.0
    if cited is not None:
        return cited, UNVERIFIED_SURFACE_FACTOR
    return None


def _add_llm_entities(
    entities: Sequence[ExtractedEntity], batch: dict[int, ChunkRow], out: _Writes
) -> None:
    for ent in entities:
        keyed = entity_key(ent)
        located = _locate(ent.surface_text, ent.chunk_ref, batch)
        if keyed is None or located is None:
            continue
        key, attrs = keyed
        name = key if ent.type == "phone" else ent.name.strip()
        out.entity(ent.type, key, name, attrs)
        chunk, factor = located
        out.mention(ent.type, key, chunk.id, ent.surface_text, ent.confidence * factor)


def _event_chunks(event: ExtractedEvent, batch: dict[int, ChunkRow]) -> list[ChunkRow]:
    chunks = [batch[r] for r in dict.fromkeys(event.chunk_refs) if r in batch]
    if chunks:
        return chunks
    if len(batch) == 1:
        return list(batch.values())
    if event.time_text:
        needle = event.time_text.casefold()
        return [c for c in batch.values() if needle in c.text.casefold()][:1]
    return []


def _add_llm_events(
    events: Sequence[ExtractedEvent],
    batch: dict[int, ChunkRow],
    recorded_at: datetime | None,
    out: _Writes,
    counts: dict[str, int],
) -> None:
    tz = case_timezone()
    for ev in events:
        chunks = _event_chunks(ev, batch)
        if not chunks:
            counts["events_unsourced"] += 1
            continue
        resolved = resolve_event_time(
            iso_start=ev.t_start,
            iso_end=ev.t_end,
            time_text=ev.time_text,
            happening_now=ev.happening_now,
            chunk_t_starts=[c.t_start for c in chunks],
            chunk_t_ends=[c.t_end for c in chunks],
            chunk_texts=[c.text for c in chunks],
            recorded_at=recorded_at,
            tz=tz,
        )
        if resolved is None:
            counts["events_unresolved_time"] += 1
            continue
        location: tuple[str, str] | None = None
        if ev.location and (loc_key := normalize_name(ev.location)):
            location = (ev.location.strip(), loc_key)
            out.entity("location", loc_key, ev.location.strip(), {})
            for chunk in chunks:
                if ev.location.casefold() in chunk.text.casefold():
                    out.mention("location", loc_key, chunk.id, ev.location, ev.confidence)
                    break
        out.events.append(
            _EventOut(
                description=ev.description.strip(),
                t_start=resolved.t_start,
                t_end=resolved.t_end,
                location=location,
                chunk_ids=tuple(c.id for c in chunks),
                confidence=ev.confidence,
                needs_review=ev.confidence < REVIEW_THRESHOLD or resolved.needs_review,
            )
        )


def _not_configured(exc: Exception) -> str | None:
    message = str(exc)
    return message if any(marker in message for marker in _NOT_CONFIGURED) else None


def _preflight(router: ModelRouter) -> str | None:
    s = router.settings
    if s.llm_provider == "gemini" and s.gemini_api_key is None:
        return "CASELENS_GEMINI_API_KEY is not set"
    return None


def _llm_pass(
    ctx: StageContext,
    router: ModelRouter,
    chunks: Sequence[ChunkRow],
    out: _Writes,
    counts: dict[str, int],
) -> str:
    """Run the extraction calls. Returns "ok" or "skipped: <reason>"."""
    from caselens_agent.router import ModelError, evidence_block

    reason = _preflight(router)
    if reason is not None:
        return f"skipped: {reason}"
    by_index = {c.index: c for c in chunks}
    tz = case_timezone()
    # Times are shown to the model in the case timezone, as a detective would read them.
    prompt_chunks = [
        PromptChunk(c.index, c.text, c.t_start.astimezone(tz) if c.t_start else None)
        for c in chunks
    ]
    recorded = ctx.evidence.recorded_at
    for batch in batch_chunks(prompt_chunks):
        user = extract_user_prompt(
            batch,
            evidence_type=ctx.evidence.type,
            reference=recorded.astimezone(tz) if recorded is not None else None,
            timezone=str(tz),
            block=evidence_block,
        )
        try:
            result = router.complete_json("extract", EXTRACT_SYSTEM, user, ExtractionOutput)
        except ModelError as exc:
            if (why := _not_configured(exc)) is not None:
                return f"skipped: {why}"
            raise
        counts["llm_calls"] += 1
        batch_rows = {c.chunk_index: by_index[c.chunk_index] for c in batch}
        _add_llm_entities(result.entities, batch_rows, out)
        _add_llm_events(result.events, batch_rows, ctx.evidence.recorded_at, out, counts)
    return "ok"


def _excerpt(conn: Conn, entity_id: uuid.UUID, width: int = 600) -> str:
    row = conn.execute(
        "SELECT c.text, m.surface_text FROM public.mentions m JOIN public.chunks c "
        "ON c.id = m.chunk_id WHERE m.entity_id = %s "
        "ORDER BY m.confidence DESC NULLS LAST, m.created_at LIMIT 1",
        (entity_id,),
    ).fetchone()
    if row is None or not row[0]:
        return ""
    text, surface = str(row[0]), str(row[1])
    at = max(text.casefold().find(surface.casefold()), 0)
    start = max(at - width // 2, 0)
    return text[start : start + width]


def _make_adjudicator(
    conn: Conn, router: ModelRouter | None, counts: dict[str, int]
) -> Callable[[Candidate, Candidate], Verdict | None]:
    def adjudicate(newer: Candidate, older: Candidate) -> Verdict | None:
        if router is None:
            return None
        from caselens_agent.router import ModelError, evidence_block

        user = adjudicate_user_prompt(
            EntityContext(newer.type, newer.name, newer.attributes, _excerpt(conn, newer.id)),
            EntityContext(older.type, older.name, older.attributes, _excerpt(conn, older.id)),
            evidence_block,
        )
        try:
            out = router.complete_json("adjudicate", ADJUDICATE_SYSTEM, user, AdjudicationOutput)
        except ModelError as exc:
            if _not_configured(exc) is not None:
                return None
            raise
        counts["llm_calls"] += 1
        same = out.same_entity and out.confidence >= ADJUDICATION_MIN_CONFIDENCE
        return Verdict(same=same, confidence=out.confidence)

    return adjudicate


def _write(ctx: StageContext, out: _Writes) -> set[uuid.UUID]:
    """Replace this evidence's mentions/events and write the new ones; returns entity ids."""
    conn, case_id = ctx.conn, ctx.evidence.case_id
    lock_case(conn, case_id)
    conn.execute(
        "DELETE FROM public.mentions WHERE chunk_id IN "
        "(SELECT id FROM public.chunks WHERE evidence_id = %s)",
        (ctx.evidence.id,),
    )
    conn.execute(
        "DELETE FROM public.events WHERE case_id = %s AND cardinality(source_chunk_ids) > 0 "
        "AND source_chunk_ids <@ "
        "(SELECT coalesce(array_agg(id), '{}') FROM public.chunks WHERE evidence_id = %s)",
        (case_id, ctx.evidence.id),
    )
    ids: dict[tuple[EntityType, str], uuid.UUID] = {}
    for (kind, key), (name, attrs) in out.entities.items():
        entity_id = ensure_entity(conn, case_id, kind, name, key, attrs)
        merge_attributes(conn, entity_id, attrs)
        ids[(kind, key)] = entity_id
    for (kind, key, chunk_id, _), (surface, confidence) in out.mentions.items():
        add_mention(
            conn,
            case_id,
            ids[(kind, key)],
            chunk_id,
            surface,
            round(confidence, 4),
            REVIEW_THRESHOLD,
        )
    seen: set[tuple[str, datetime]] = set()
    for ev in out.events:
        dedupe = (ev.description.casefold(), ev.t_start)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        location_id = ids[("location", ev.location[1])] if ev.location else None
        conn.execute(
            "INSERT INTO public.events (case_id, t_start, t_end, location_id, description, "
            "source_chunk_ids, confidence, needs_review) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                case_id,
                ev.t_start,
                ev.t_end,
                location_id,
                ev.description,
                list(ev.chunk_ids),
                round(ev.confidence, 4),
                ev.needs_review,
            ),
        )
    return set(ids.values())


def run_extract(ctx: StageContext, router: Callable[[], ModelRouter]) -> StageResult:
    chunks = _load_chunks(ctx.conn, ctx.evidence.id)
    counts = {"llm_calls": 0, "events_unresolved_time": 0, "events_unsourced": 0}
    out = _Writes()
    times_seen = _deterministic(chunks, out)
    llm_router: ModelRouter | None = router() if chunks else None
    llm = _llm_pass(ctx, llm_router, chunks, out, counts) if llm_router else "skipped: no chunks"
    with ctx.conn.transaction():
        entity_ids = _write(ctx, out)
        adjudicator = _make_adjudicator(ctx.conn, llm_router if llm == "ok" else None, counts)
        links = link_similar_entities(ctx.conn, ctx.evidence.case_id, entity_ids, adjudicator)
    review = sum(1 for _, conf in out.mentions.values() if conf < REVIEW_THRESHOLD)
    return StageResult(
        summary={
            "chunks": len(chunks),
            "llm": llm,
            "llm_calls": counts["llm_calls"],
            "entities": len(entity_ids),
            "mentions": len(out.mentions),
            "mentions_needs_review": review,
            "events": len({(e.description.casefold(), e.t_start) for e in out.events}),
            "events_unresolved_time": counts["events_unresolved_time"],
            "events_unsourced": counts["events_unsourced"],
            "times_seen": times_seen,
            "links_fuzzy": links.fuzzy,
            "links_llm": links.llm,
            "adjudicated": links.adjudicated,
            "adjudication_skipped": links.adjudication_skipped,
        }
    )


def make_extract_stage(
    router: ModelRouter | None = None,
) -> Callable[[StageContext], StageResult]:
    """Build the stage. With no router, a default `ModelRouter()` is built on first use."""
    holder: list[ModelRouter] = [router] if router is not None else []

    def get_router() -> ModelRouter:
        if not holder:
            from caselens_agent.router import ModelRouter

            holder.append(ModelRouter())
        return holder[0]

    def extract(ctx: StageContext) -> StageResult:
        return run_extract(ctx, get_router)

    return extract


extract = make_extract_stage()
