"""Tests for the SYNTHETIC structured-data (CSV) generator."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from caselens_data_gen.common import SYNTHETIC_NOTICE, GeneratedFile
from caselens_data_gen.story import STORY_SLUGS, Story, load_story
from caselens_data_gen.story.models import PHONE_PATTERN
from caselens_data_gen.structured import generate
from caselens_data_gen.structured._shared import iso, shared_phone_numbers

SHARED_PHONE = "+13605550188"
COUNTS = {
    "riverside_lot": {"call_records": 500, "plate_reads": 300, "custody_records": 40},
    "harbor_street": {"call_records": 60, "plate_reads": 40, "custody_records": 6},
}
FILES = {
    "call_records": "call_detail_records.csv",
    "plate_reads": "plate_reader_hits.csv",
    "custody_records": "evidence_log.csv",
}
HEADERS = {
    "call_records": [
        "row_id",
        "caller_number",
        "callee_number",
        "started_at",
        "duration_s",
        "call_type",
        "cell_tower_id",
    ],
    "plate_reads": [
        "row_id",
        "plate",
        "plate_state",
        "read_at",
        "camera_id",
        "location_name",
        "latitude",
        "longitude",
        "confidence",
    ],
    "custody_records": [
        "row_id",
        "item_id",
        "description",
        "collected_by",
        "collected_at",
        "collected_location",
        "custody_chain",
    ],
}


class Case:
    def __init__(self, story: Story, case_dir: Path, files: list[GeneratedFile]) -> None:
        self.story = story
        self.case_dir = case_dir
        self.files = {f.path.name: f for f in files}
        self.rows: dict[str, list[dict[str, str]]] = {}
        for table, name in FILES.items():
            with (case_dir / "csv" / name).open(encoding="utf-8", newline="") as handle:
                lines = [line for line in handle if not line.startswith("#")]
            self.rows[table] = list(csv.DictReader(lines))

    def row(self, table: str, row_id: int) -> dict[str, str]:
        found = self.rows[table][row_id - 1]
        assert found["row_id"] == str(row_id)
        return found

    def locator_rows(self, table: str, fact_id: str) -> list[int]:
        facts = self.files[FILES[table]].facts
        out = []
        for fact in facts:
            if fact.fact_id == fact_id:
                assert fact.locator["kind"] == "record"
                assert fact.locator["table"] == table
                out.append(int(fact.locator["row_id"]))
        return out


@pytest.fixture(scope="module", params=STORY_SLUGS)
def case(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Case:
    slug: str = request.param
    story = load_story(slug)
    case_dir = tmp_path_factory.mktemp(slug)
    return Case(story, case_dir, generate(story, case_dir))


@pytest.fixture(scope="module")
def riverside(tmp_path_factory: pytest.TempPathFactory) -> Case:
    story = load_story("riverside_lot")
    case_dir = tmp_path_factory.mktemp("riverside_only")
    return Case(story, case_dir, generate(story, case_dir))


def test_files_header_and_counts(case: Case) -> None:
    assert set(case.files) == set(FILES.values())
    for table, name in FILES.items():
        path = case.case_dir / "csv" / name
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# " + SYNTHETIC_NOTICE
        assert lines[1].split(",") == HEADERS[table]
        assert len(case.rows[table]) == COUNTS[case.story.case_slug][table]
        assert [r["row_id"] for r in case.rows[table]] == [
            str(i) for i in range(1, len(case.rows[table]) + 1)
        ]
        assert case.files[name].evidence_type == "csv"


def test_deterministic(case: Case, tmp_path: Path) -> None:
    generate(case.story, tmp_path)
    for name in FILES.values():
        assert (tmp_path / "csv" / name).read_bytes() == (case.case_dir / "csv" / name).read_bytes()


def test_story_calls_at_their_rows(case: Case) -> None:
    number = {p.id: p.number for p in case.story.phones}
    for call in case.story.calls:
        (row_id,) = case.locator_rows("call_records", call.id)
        row = case.row("call_records", row_id)
        assert row["caller_number"] == number[call.caller_phone_id]
        assert row["callee_number"] == number[call.callee_phone_id]
        assert datetime.fromisoformat(row["started_at"]) == call.started_at
        assert row["started_at"] == iso(call.started_at)
        assert row["duration_s"] == str(call.duration_s)
        assert row["call_type"] == call.call_type
        assert row["cell_tower_id"] == (call.cell_tower_id or "")
        if call.event_id:
            assert row_id in case.locator_rows("call_records", call.event_id)


def test_story_reads_at_their_rows(case: Case) -> None:
    cameras = {c.id: c for c in case.story.cameras}
    for read in case.story.plate_reads:
        (row_id,) = case.locator_rows("plate_reads", read.id)
        row = case.row("plate_reads", row_id)
        cam = cameras[read.camera_id]
        assert row["plate"] == read.plate
        assert row["plate_state"] == read.plate_state
        assert datetime.fromisoformat(row["read_at"]) == read.read_at
        assert row["camera_id"] == cam.id
        assert row["location_name"] == cam.name
        assert float(row["latitude"]) == cam.latitude
        assert float(row["longitude"]) == cam.longitude
        assert float(row["confidence"]) == read.confidence
        if read.event_id:
            assert row_id in case.locator_rows("plate_reads", read.event_id)


def test_story_items_at_their_rows(case: Case) -> None:
    for item in case.story.evidence_items:
        (row_id,) = case.locator_rows("custody_records", item.item_id)
        row = case.row("custody_records", row_id)
        assert row["item_id"] == item.item_id
        assert row["description"] == item.description
        assert datetime.fromisoformat(row["collected_at"]) == item.collected_at
        assert item.location_detail in row["collected_location"]
        assert row["custody_chain"].count(" | ") == len(item.custody_chain) - 1
        for step in item.custody_chain:
            assert step.action in row["custody_chain"]


def test_rows_chronological(case: Case) -> None:
    for table, column in (("call_records", "started_at"), ("plate_reads", "read_at")):
        times = [datetime.fromisoformat(r[column]) for r in case.rows[table]]
        assert times == sorted(times)
        assert all(t.tzinfo is not None for t in times)


def test_noise_never_links_story_phones(case: Case) -> None:
    story_numbers = {p.number for p in case.story.phones}
    story_rows = {f.locator["row_id"] for f in case.files[FILES["call_records"]].facts}
    for row in case.rows["call_records"]:
        caller, callee = row["caller_number"], row["callee_number"]
        assert PHONE_PATTERN.match(caller) and PHONE_PATTERN.match(callee)
        if int(row["row_id"]) in story_rows:
            continue
        assert not {caller, callee} <= story_numbers, row
        assert SHARED_PHONE not in (caller, callee), row
    assert SHARED_PHONE in shared_phone_numbers()


def test_noise_plates_are_synthetic_and_distinct(case: Case) -> None:
    story_plates = {r.plate for r in case.story.plate_reads} | {
        v.plate for v in case.story.vehicles
    }
    story_rows = {f.locator["row_id"] for f in case.files[FILES["plate_reads"]].facts}
    for row in case.rows["plate_reads"]:
        assert row["plate"].startswith("SYN")
        assert 0.0 <= float(row["confidence"]) <= 1.0
        if int(row["row_id"]) not in story_rows:
            assert row["plate"] not in story_plates


def test_kemper_hale_seven_calls_in_week_before(riverside: Case) -> None:
    owner = {p.number: p.owner_id for p in riverside.story.phones}
    shooting = datetime.fromisoformat("2026-03-14T22:44:20-07:00")
    pair = {"p_suspect", "p_victim"}
    for week_start in ("2026-03-07T22:44:20-08:00", "2026-03-07T00:00:00-08:00"):
        start = datetime.fromisoformat(week_start)
        hits = [
            r
            for r in riverside.rows["call_records"]
            if {owner.get(r["caller_number"]), owner.get(r["callee_number"])} == pair
            and start <= datetime.fromisoformat(r["started_at"]) <= shooting
        ]
        assert len(hits) == 7


def test_scene_night_has_only_story_reads(riverside: Case) -> None:
    lo = datetime.fromisoformat("2026-03-14T21:35:00-07:00")
    hi = datetime.fromisoformat("2026-03-14T23:15:00-07:00")
    at_lot = [
        r
        for r in riverside.rows["plate_reads"]
        if r["camera_id"] == "alpr_riverlot_entrance"
        and lo <= datetime.fromisoformat(r["read_at"]) <= hi
    ]
    assert {r["plate"] for r in at_lot} == {"SYN7305", "SYN2290", "SYN4821"}


def test_evidence_log_event_links(riverside: Case) -> None:
    rows = riverside.locator_rows("custody_records", "ev_26")
    items = {riverside.row("custody_records", r)["item_id"] for r in rows}
    assert "ITEM-001" in items and "ITEM-015" not in items
    rows = riverside.locator_rows("custody_records", "ev_29")
    items = {riverside.row("custody_records", r)["item_id"] for r in rows}
    assert "ITEM-023" in items
