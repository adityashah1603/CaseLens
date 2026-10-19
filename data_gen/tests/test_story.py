"""Validates the SYNTHETIC ground-truth stories against the project guide's dataset spec."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from caselens_data_gen.story import STORY_SLUGS, load_story
from caselens_data_gen.story.models import PHONE_PATTERN, Story

NORTH_STAR = "Who was in the gray sedan on March 14?"


@pytest.fixture(scope="module", params=STORY_SLUGS)
def story(request: pytest.FixtureRequest) -> Story:
    slug: str = request.param
    return load_story(slug)


@pytest.fixture(scope="module")
def riverside() -> Story:
    return load_story("riverside_lot")


@pytest.fixture(scope="module")
def harbor() -> Story:
    return load_story("harbor_street")


def all_timestamps(s: Story) -> list[datetime]:
    out: list[datetime] = []
    out += [t for e in s.events for t in (e.t_start, e.t_end)]
    out += [c.started_at for c in s.calls]
    out += [r.read_at for r in s.plate_reads]
    out += [p.recorded_at for p in s.evidence_plan]
    out += [tr.t_start for p in s.evidence_plan for tr in p.tracks]
    out += [tr.t_end for p in s.evidence_plan for tr in p.tracks]
    out += [w.start for c in s.cameras for w in c.clips]
    out += [w.end for c in s.cameras for w in c.clips]
    out += [i.collected_at for i in s.evidence_items]
    out += [st.at for i in s.evidence_items for st in i.custody_chain]
    return out


def test_story_parses_and_is_synthetic(story: Story) -> None:
    assert "SYNTHETIC" in story.notice
    assert story.city == "Port Calder"
    assert story.timezone == "America/Los_Angeles"
    assert story.summary


def test_references_resolve(story: Story) -> None:
    assert story.reference_errors() == []


def test_every_event_has_existing_evidence(story: Story) -> None:
    plan_ids = {p.id for p in story.evidence_plan}
    for event in story.events:
        assert event.evidence, event.id
        assert set(event.evidence) <= plan_ids, event.id


def test_phones_are_fictional(story: Story) -> None:
    for phone in story.phones:
        assert PHONE_PATTERN.match(phone.number), phone.number


def test_plates_are_fake(story: Story) -> None:
    for vehicle in story.vehicles:
        assert vehicle.plate.startswith("SYN"), vehicle.plate
    for read in story.plate_reads:
        assert read.plate.startswith("SYN"), read.plate


def test_timestamps_are_aware_in_march_2026(story: Story) -> None:
    tz = ZoneInfo(story.timezone)
    stamps = all_timestamps(story)
    assert stamps
    for ts in stamps:
        assert ts.utcoffset() is not None
        local = ts.astimezone(tz)
        assert (local.year, local.month) == (2026, 3), ts
        # Offsets must match the case timezone (PST before 2026-03-08, PDT after).
        assert ts.utcoffset() == local.utcoffset(), ts


def test_exactly_one_shared_phone(riverside: Story, harbor: Story) -> None:
    shared = {p.number for p in riverside.phones} & {p.number for p in harbor.phones}
    assert len(shared) == 1
    assert {p.name for p in riverside.people}.isdisjoint({p.name for p in harbor.people})


def test_riverside_counts(riverside: Story) -> None:
    cast = Counter(p.role for p in riverside.cast())
    assert 6 <= sum(cast.values()) <= 8
    assert cast["victim"] == 1
    assert cast["suspect"] == 1
    assert cast["witness"] >= 2
    assert cast["red_herring"] >= 1
    assert any(p.role == "dispatcher" for p in riverside.people)
    assert len(riverside.vehicles) == 3
    assert len(riverside.phones) == 5
    assert len(riverside.locations) == 4
    assert 20 <= len(riverside.events) <= 30
    assert 10 <= len(riverside.calls) <= 20
    assert 8 <= len(riverside.plate_reads) <= 15
    assert len(riverside.evidence_items) == 40


def test_riverside_evidence_plan_matches_guide(riverside: Story) -> None:
    kinds = Counter(p.kind for p in riverside.evidence_plan)
    assert kinds["incident_report"] == 1
    assert kinds["supplemental_report"] == 3
    assert kinds["witness_statement"] + kinds["witness_statement_scanned"] == 4
    assert kinds["witness_statement_scanned"] == 1
    assert sum(p.inconsistent for p in riverside.evidence_plan) == 1
    assert kinds["911_call"] == 1
    assert kinds["suspect_interview"] == 1
    assert 4 <= kinds["cctv_clip"] <= 6
    assert kinds["call_detail_records"] == 1
    assert kinds["plate_reader_hits"] == 1
    assert kinds["evidence_log"] == 1
    by_kind = {p.kind: p for p in riverside.evidence_plan}
    assert by_kind["911_call"].duration_s is not None
    assert 90 <= by_kind["911_call"].duration_s <= 150
    assert by_kind["suspect_interview"].duration_s is not None
    assert 240 <= by_kind["suspect_interview"].duration_s <= 360


def test_gray_sedan_conflict(riverside: Story) -> None:
    inconsistent = {p.id for p in riverside.evidence_plan if p.inconsistent}
    plan_kinds = {p.id: p.kind for p in riverside.evidence_plan}
    found = False
    for cf in riverside.conflicts:
        for wrong, right in ((cf.claim_a, cf.claim_b), (cf.claim_b, cf.claim_a)):
            if wrong.evidence_id in inconsistent:
                assert "10:15" in wrong.text and "blue" in wrong.text.lower()
                assert "22:41" in right.text and "gray" in right.text.lower()
                assert plan_kinds[right.evidence_id] in {"cctv_clip", "plate_reader_hits"}
                found = True
    assert found


def test_gray_sedan_ground_truth(riverside: Story) -> None:
    sedan = next(v for v in riverside.vehicles if v.id == "v_gray_sedan")
    assert sedan.color == "gray"
    assert "the gray Civic" in sedan.aliases
    reads = sorted(r.read_at for r in riverside.plate_reads if r.plate == sedan.plate)
    window = [r for r in reads if r.isoformat().startswith("2026-03-14T22")]
    assert any(r.strftime("%H:%M:%S") == "22:41:05" for r in window)
    assert len(window) >= 4


def test_calls_between_suspect_and_victim_week_before(riverside: Story) -> None:
    owners = {p.id: p.owner_id for p in riverside.phones}
    pair = {"p_suspect", "p_victim"}
    between = [
        c for c in riverside.calls if {owners[c.caller_phone_id], owners[c.callee_phone_id]} == pair
    ]
    shooting = datetime.fromisoformat("2026-03-14T22:44:20-07:00")
    # Same answer whether "the week before" means 7x24h back or since midnight on March 7.
    for week_start in ("2026-03-07T22:44:20-08:00", "2026-03-07T00:00:00-08:00"):
        start = datetime.fromisoformat(week_start)
        assert sum(start <= c.started_at <= shooting for c in between) == 7
    assert any(c.started_at < datetime.fromisoformat("2026-03-07T00:00:00-08:00") for c in between)


def test_questions(story: Story) -> None:
    assert len(story.unanswerable) >= 2
    for q in story.unanswerable:
        assert q.question.endswith("?")
        assert q.reason


def test_riverside_questions(riverside: Story) -> None:
    assert NORTH_STAR in riverside.demo_questions
    assert len(riverside.demo_questions) == 5
    assert len(riverside.unanswerable) >= 5


def test_harbor_is_small(harbor: Story) -> None:
    assert 3 <= len(harbor.cast()) <= 4
    assert len(harbor.vehicles) == 1
    assert len(harbor.phones) == 2
    assert len(harbor.locations) == 2
    assert 5 <= len(harbor.events) <= 8
    kinds = {p.kind for p in harbor.evidence_plan}
    assert {"call_detail_records", "evidence_log"} <= kinds
