"""Entity resolution, deterministic patterns and time resolution. All names SYNTHETIC
(fictional Riverside Lot story)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from caselens_workers.db import Conn
from caselens_workers.extraction.patterns import find_dates, find_phones, find_plates, find_times
from caselens_workers.extraction.prompts import PromptChunk, batch_chunks
from caselens_workers.extraction.schemas import EntityAttributes, ExtractedEntity
from caselens_workers.extraction.timeres import (
    DEFAULT_CASE_TIMEZONE,
    case_timezone,
    resolve_event_time,
)
from caselens_workers.resolve import (
    Candidate,
    Verdict,
    ensure_entity,
    link_similar_entities,
    merge_attributes,
    name_score,
    normalize_name,
    normalize_phone,
    normalize_plate,
)
from caselens_workers.stages.extract import entity_key
from caselens_workers.testing import SeededEvidence, as_role

LA = ZoneInfo("America/Los_Angeles")


def test_normalizers() -> None:
    assert normalize_phone("(360) 555-0142") == "+13605550142"
    assert normalize_phone("1-360-555-0142") == "+13605550142"
    assert normalize_phone("555-0142") is None
    assert normalize_plate("syn 4821") == "SYN4821"
    assert normalize_name("The  Riverside Lot.") == "riverside lot"
    assert normalize_name("T. Kemper") == "t kemper"
    assert normalize_name("Calder Fuel & Go") == "calder fuel and go"
    assert normalize_name("...") is None


def test_find_phones_and_plates() -> None:
    text = (
        "SYNTHETIC: call (360) 555-0142, 253.555.0166 or +1 971 555 0178; case 26-031487. "
        "Civic SYN 4821, pickup SYN-7305, tag ABC1234; badge PCPD 0871, ITEM-001, ET-17."
    )
    assert [p.value for p in find_phones(text)] == ["+13605550142", "+12535550166", "+19715550178"]
    assert [(p.value, p.confidence) for p in find_plates(text)] == [
        ("SYN4821", 1.0),
        ("SYN7305", 1.0),
        ("ABC1234", 0.5),
    ]


def test_find_dates_and_times() -> None:
    text = (
        "SYNTHETIC. On March 14, 2026 around 10:15 PM; 22:10-22:19; taken 03/15/2026 16:00; "
        "read 2026-03-14T22:41:05-07:00; arrested 16 March; about 11 p.m."
    )
    assert [d.with_year(2026) for d in find_dates(text)] == [
        date(2026, 3, 14),
        date(2026, 3, 15),
        date(2026, 3, 14),
        date(2026, 3, 16),
    ]
    assert find_dates("16 March")[0].year is None
    assert find_times(text) == [
        time(22, 15),
        time(22, 10),
        time(22, 19),
        time(16, 0),
        time(22, 41, 5),  # the ISO offset "-07:00" is not a clock time
        time(23, 0),
    ]


def _resolve(**kwargs: object) -> tuple[str, str | None, bool, str] | None:
    args: dict[str, object] = {
        "iso_start": None,
        "iso_end": None,
        "time_text": None,
        "happening_now": False,
        "chunk_t_starts": [None],
        "chunk_t_ends": [None],
        "chunk_texts": [""],
        "recorded_at": None,
        "tz": LA,
    }
    args.update(kwargs)
    got = resolve_event_time(**args)  # type: ignore[arg-type]
    if got is None:
        return None
    end = got.t_end.isoformat() if got.t_end else None
    return got.t_start.isoformat(), end, got.needs_review, got.basis


def test_time_resolution_order() -> None:
    recorded = datetime(2026, 3, 15, 16, 0, tzinfo=LA)
    # Model ISO wins; naive values are read in the case timezone.
    assert _resolve(iso_start="2026-03-14T22:44:20", recorded_at=recorded) == (
        "2026-03-14T22:44:20-07:00",
        None,
        False,
        "iso",
    )
    # Explicit date + time in the text.
    assert _resolve(time_text="March 14, 2026 at 10:41 PM", recorded_at=recorded) == (
        "2026-03-14T22:41:00-07:00",
        None,
        False,
        "text",
    )
    # Time of day + recorded_at date; a range gives t_end.
    assert _resolve(time_text="22:10-22:19", recorded_at=recorded) == (
        "2026-03-15T22:10:00-07:00",
        "2026-03-15T22:19:00-07:00",
        False,
        "reference",
    )
    # Time of day + the chunk's own wall-clock date beats recorded_at.
    chunk_start = datetime(2026, 3, 14, 23, 5, tzinfo=LA)
    assert _resolve(time_text="10:15 PM", chunk_t_starts=[chunk_start], recorded_at=recorded)
    assert _resolve(time_text="10:15 PM", chunk_t_starts=[chunk_start])[0] == (  # type: ignore[index]
        "2026-03-14T22:15:00-07:00"
    )
    # Neither: the only date written in the source chunk.
    assert _resolve(time_text="11 p.m.", chunk_texts=["SYNTHETIC log for 03/14/2026"]) == (
        "2026-03-14T23:00:00-07:00",
        None,
        False,
        "reference",
    )
    # Live narration uses the chunk's t_start.
    assert _resolve(happening_now=True, chunk_t_starts=[chunk_start]) == (
        "2026-03-14T23:05:00-07:00",
        None,
        False,
        "live",
    )
    # Date only: local midnight, flagged.
    assert _resolve(time_text="on 16 March", recorded_at=recorded) == (
        "2026-03-16T00:00:00-07:00",
        None,
        True,
        "date_only",
    )
    assert _resolve(time_text="later that night") is None
    assert _resolve(time_text="10:15 PM") is None  # no date anywhere


def test_case_timezone_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASELENS_CASE_TIMEZONE", raising=False)
    assert str(case_timezone()) == DEFAULT_CASE_TIMEZONE
    monkeypatch.setenv("CASELENS_CASE_TIMEZONE", "America/New_York")
    assert str(case_timezone()) == "America/New_York"


def test_batching_respects_budget() -> None:
    chunks = [PromptChunk(i, "x" * 1500, None) for i in range(5)] + [
        PromptChunk(5, "", None),
        PromptChunk(6, "y" * 5000, None),
    ]
    assert [[c.chunk_index for c in b] for b in batch_chunks(chunks, 4000)] == [
        [0, 1],
        [2, 3],
        [4],
        [6],
    ]


def test_name_scores() -> None:
    assert name_score("person", "t kemper", "travis kemper") >= 0.92
    assert name_score("person", "kemper", "travis kemper") >= 0.92
    assert 0.75 <= name_score("person", "mark hale", "marcus hale") < 0.92
    assert name_score("person", "wes darnell", "marcus hale") < 0.75
    assert 0.75 <= name_score("location", "riverside market", "riverside lot") < 0.92


def _llm_entity(type_: str, name: str, **attrs: str) -> ExtractedEntity:
    return ExtractedEntity(
        type=type_,  # type: ignore[arg-type]
        name=name,
        attributes=EntityAttributes(**attrs),
        surface_text=name,
        chunk_ref=0,
        confidence=0.9,
    )


def test_entity_keys() -> None:
    assert entity_key(_llm_entity("phone", "360-555-0142")) == ("+13605550142", {})
    assert entity_key(_llm_entity("phone", "unknown")) is None
    key, attrs = entity_key(_llm_entity("vehicle", "Civic SYN 4821", color="gray")) or ("", {})
    assert key == "SYN4821" and attrs == {"color": "gray", "plate": "SYN4821"}
    assert entity_key(_llm_entity("vehicle", "the blue Camry"))[0] == "desc:blue camry"  # type: ignore[index]
    assert entity_key(_llm_entity("location", "The Riverside Lot"))[0] == "riverside lot"  # type: ignore[index]


# ---------------------------------------------------------------- DB: passes 1-3


@pytest.fixture
def case_id(
    owner_conn: Conn, seed_evidence: Callable[..., SeededEvidence], tmp_path: Path
) -> uuid.UUID:
    f = tmp_path / "x.pdf"
    f.write_text("SYNTHETIC", encoding="utf-8")
    cid = seed_evidence(f, "pdf").case_id
    as_role(owner_conn, "caselens_worker")
    return cid


@pytest.mark.db
def test_exact_pass_reuses_and_merges_attributes(owner_conn: Conn, case_id: uuid.UUID) -> None:
    a = ensure_entity(owner_conn, case_id, "vehicle", "SYN4821", "SYN4821", {"plate": "SYN4821"})
    b = ensure_entity(owner_conn, case_id, "vehicle", "gray Honda Civic", "SYN4821")
    assert a == b
    merge_attributes(owner_conn, a, {"color": "gray", "plate": "XXX", "aliases": ["the Civic"]})
    merge_attributes(owner_conn, a, {"aliases": ["the Civic", "gray sedan"]})
    row = owner_conn.execute(
        "SELECT attributes FROM public.entities WHERE id = %s", (a,)
    ).fetchone()
    assert row == ({"plate": "SYN4821", "color": "gray", "aliases": ["the Civic", "gray sedan"]},)


def _links(conn: Conn, case_id: uuid.UUID) -> list[tuple[str, str, str]]:
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT f.canonical_name, t.canonical_name, l.method FROM public.entity_links l "
            "JOIN public.entities f ON f.id = l.from_entity_id "
            "JOIN public.entities t ON t.id = l.to_entity_id WHERE l.case_id = %s "
            "ORDER BY l.created_at, f.canonical_name",
            (case_id,),
        ).fetchall()
    ]


@pytest.mark.db
def test_fuzzy_links_newer_to_older_once(owner_conn: Conn, case_id: uuid.UUID) -> None:
    names = ["Travis Kemper", "T. Kemper", "Kemper", "Marcus Hale", "Wes Darnell"]
    ids = [ensure_entity(owner_conn, case_id, "person", n, normalize_name(n)) for n in names]
    stats = link_similar_entities(owner_conn, case_id, ids)
    assert (stats.fuzzy, stats.llm, stats.adjudicated) == (2, 0, 0)
    assert sorted(_links(owner_conn, case_id)) == [
        ("Kemper", "Travis Kemper", "fuzzy"),
        ("T. Kemper", "Travis Kemper", "fuzzy"),
    ]
    again = link_similar_entities(owner_conn, case_id, ids)
    assert again.fuzzy == 0 and len(_links(owner_conn, case_id)) == 2


@pytest.mark.db
def test_ambiguous_and_band_scores_are_adjudicated(owner_conn: Conn, case_id: uuid.UUID) -> None:
    seen: list[tuple[str, str]] = []

    def adjudicate(newer: Candidate, older: Candidate) -> Verdict | None:
        seen.append((newer.name, older.name))
        return Verdict(same=newer.name == "Mark Hale", confidence=0.7)

    names = ["Marcus Hale", "Carla Hale", "Mark Hale", "Hale"]
    ids = [ensure_entity(owner_conn, case_id, "person", n, normalize_name(n)) for n in names]
    stats = link_similar_entities(owner_conn, case_id, ids, adjudicate)
    # "Mark Hale" is in the adjudication band; "Hale" matches two different Hales above the
    # fuzzy threshold, so it is adjudicated against the oldest instead of auto-linked.
    assert seen == [("Mark Hale", "Marcus Hale"), ("Hale", "Marcus Hale")]
    assert (stats.fuzzy, stats.adjudicated, stats.llm) == (0, 2, 1)
    assert _links(owner_conn, case_id) == [("Mark Hale", "Marcus Hale", "llm")]
    # A full name never auto-links to an older bare surname.
    dana = ensure_entity(owner_conn, case_id, "person", "Dana Hale", "dana hale")
    capped = link_similar_entities(owner_conn, case_id, [dana])
    assert capped.fuzzy == 0 and capped.adjudication_skipped == 1
    # Without an adjudicator the band is skipped, not linked.
    other = ensure_entity(owner_conn, case_id, "location", "Riverside Market", "riverside market")
    lot = ensure_entity(owner_conn, case_id, "location", "Riverside Lot", "riverside lot")
    skipped = link_similar_entities(owner_conn, case_id, [other, lot])
    assert skipped.adjudication_skipped == 1 and skipped.fuzzy == 0


@pytest.mark.db
def test_vehicle_attribute_conflict_blocks_link(owner_conn: Conn, case_id: uuid.UUID) -> None:
    gray = ensure_entity(owner_conn, case_id, "vehicle", "Honda Civic", "desc:a", {"color": "gray"})
    blue = ensure_entity(owner_conn, case_id, "vehicle", "Honda Civic", "desc:b", {"color": "blue"})
    plain = ensure_entity(owner_conn, case_id, "vehicle", "the Civic", "desc:c")
    stats = link_similar_entities(owner_conn, case_id, [gray, blue, plain])
    # blue vs gray conflict; "the Civic" matches both roots strongly -> ambiguous, no adjudicator.
    assert stats.fuzzy == 0 and stats.adjudication_skipped == 1


def test_names_with_different_numbers_never_match() -> None:
    from caselens_workers.resolve import name_score

    assert (
        name_score(
            "location", "pcpd headquarters interview room 1", "pcpd headquarters interview room 2"
        )
        == 0.0
    )
    assert name_score("location", "unit 14", "larkspur court apartments unit 14") > 0.9


# ---------------------------------------------------------------- vehicle description -> plate


def test_plate_compatibility() -> None:
    from caselens_workers.resolve import plate_compatibility

    civic = {"color": "gray", "make": "Honda", "model": "Civic"}
    assert plate_compatibility({"color": "gray", "make": "Honda"}, "SYN4821", civic)
    assert plate_compatibility({"color": "gray or silver"}, "SYN4821", civic)
    assert plate_compatibility({"plate": "SYN48", "color": "grey"}, "SYN4821", civic)
    assert plate_compatibility({"plate": "SYN4821 (CA)"}, "SYN4821", {})
    assert plate_compatibility({"plate": "UNKNOWN", "make": "Honda"}, "SYN4821", civic)
    # A disagreeing color, make or plate blocks the link; no positive agreement blocks it too.
    assert not plate_compatibility({"color": "blue", "make": "Honda"}, "SYN4821", civic)
    assert not plate_compatibility({"color": "gray", "make": "Toyota"}, "SYN4821", civic)
    assert not plate_compatibility({"plate": "SYN2290"}, "SYN4821", civic)
    assert not plate_compatibility({}, "SYN4821", civic)
    assert not plate_compatibility({"color": "gray"}, "SYN4821", {})


def _chunk(conn: Conn, case_id: uuid.UUID, index: int) -> uuid.UUID:
    row = conn.execute(
        "SELECT id FROM public.evidence WHERE case_id = %s LIMIT 1", (case_id,)
    ).fetchone()
    assert row is not None
    chunk_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO public.chunks (id, case_id, evidence_id, modality, chunk_index, text, "
        "locator) VALUES (%s, %s, %s, 'pdf', %s, 'SYNTHETIC', '{\"kind\": \"pdf\", \"page\": 1}')",
        (chunk_id, case_id, row[0], index),
    )
    return chunk_id


@pytest.mark.db
def test_descriptions_link_to_their_co_mentioned_plate(
    owner_conn: Conn, case_id: uuid.UUID
) -> None:
    from caselens_workers.resolve import add_mention, link_vehicle_plates

    civic_attrs = {"plate": "SYN4821", "color": "gray", "make": "Honda", "model": "Civic"}
    camry_attrs = {"plate": "SYN2290", "color": "blue", "make": "Toyota", "model": "Camry"}
    civic = ensure_entity(owner_conn, case_id, "vehicle", "SYN4821", "SYN4821", civic_attrs)
    camry = ensure_entity(owner_conn, case_id, "vehicle", "SYN2290", "SYN2290", camry_attrs)
    gray = ensure_entity(
        owner_conn, case_id, "vehicle", "gray Honda Civic", "desc:gray honda civic",
        {"color": "gray", "make": "Honda"},
    )  # fmt: skip
    car = ensure_entity(owner_conn, case_id, "vehicle", "a car", "desc:a car", {})
    tagged = ensure_entity(
        owner_conn, case_id, "vehicle", "the Civic", "desc:the civic", {"plate": "SYN4821 (CA)"}
    )
    both = _chunk(owner_conn, case_id, 0)  # mentions both plates, the description and "a car"
    for entity in (civic, camry, gray, car):
        add_mention(owner_conn, case_id, entity, both, "x", 1.0)

    assert link_vehicle_plates(owner_conn, case_id, [gray, car, tagged]) == 2
    # gray Honda -> SYN4821 (the blue Toyota disagrees); "a car" has nothing to agree on; "the
    # Civic" names its plate in its attributes (no shared chunk needed).
    assert sorted(_links(owner_conn, case_id)) == [
        ("gray Honda Civic", "SYN4821", "fuzzy"),
        ("the Civic", "SYN4821", "fuzzy"),
    ]
    # Insert-only and idempotent; a reverted link is never re-made.
    assert link_vehicle_plates(owner_conn, case_id, [gray, car, tagged]) == 0
    # An analyst's revert (the worker role may only INSERT links, so do it as the owner here).
    role = owner_conn.execute("SELECT current_user").fetchone()
    as_role(owner_conn, None)
    owner_conn.execute(
        "UPDATE public.entity_links SET status = 'reverted', reverted_at = now() "
        "WHERE from_entity_id = %s",
        (gray,),
    )
    if role is not None and role[0] != "caselens":
        as_role(owner_conn, role[0])
    assert link_vehicle_plates(owner_conn, case_id, [gray]) == 0
    # The standalone re-link pass over the whole case finds nothing new either.
    from caselens_workers.resolve import relink_vehicles

    assert relink_vehicles(owner_conn, case_id) == 0


@pytest.mark.db
def test_a_description_next_to_two_matching_plates_is_not_linked(
    owner_conn: Conn, case_id: uuid.UUID
) -> None:
    from caselens_workers.resolve import add_mention, link_vehicle_plates

    attrs = {"color": "gray"}
    a = ensure_entity(owner_conn, case_id, "vehicle", "SYN1111", "SYN1111", attrs)
    b = ensure_entity(owner_conn, case_id, "vehicle", "SYN2222", "SYN2222", attrs)
    desc = ensure_entity(owner_conn, case_id, "vehicle", "gray car", "desc:gray car", attrs)
    chunk = _chunk(owner_conn, case_id, 0)
    for entity in (a, b, desc):
        add_mention(owner_conn, case_id, entity, chunk, "x", 1.0)
    assert link_vehicle_plates(owner_conn, case_id, [desc]) == 0
    assert _links(owner_conn, case_id) == []


@pytest.mark.db
def test_relink_vehicles_covers_the_whole_case(owner_conn: Conn, case_id: uuid.UUID) -> None:
    from caselens_workers.resolve import add_mention, relink_vehicles

    plate = ensure_entity(
        owner_conn, case_id, "vehicle", "SYN4821", "SYN4821", {"color": "gray", "make": "Honda"}
    )
    desc = ensure_entity(
        owner_conn, case_id, "vehicle", "gray car", "desc:gray car", {"color": "gray"}
    )
    chunk = _chunk(owner_conn, case_id, 0)
    for entity in (plate, desc):
        add_mention(owner_conn, case_id, entity, chunk, "x", 1.0)
    assert relink_vehicles(owner_conn, case_id) == 1
    assert relink_vehicles(owner_conn, case_id) == 0
    assert _links(owner_conn, case_id) == [("gray car", "SYN4821", "fuzzy")]
