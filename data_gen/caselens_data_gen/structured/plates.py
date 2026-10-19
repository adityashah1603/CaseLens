"""SYNTHETIC ALPR plate-reader hits: story reads embedded among deterministic noise.

Noise rules (so planted answers never change):
* noise plates start with ``SYN`` and differ from every story plate (all stories, including the
  deliberate misread) in at least two characters, so they never look like a misread of one;
* on each camera, no noise inside the span of that camera's event-linked story reads, padded by
  ``QUIET`` on both sides (the incident window shows only story vehicles).

The story's ``direction`` field has no column in the plate_reads contract, so it is not exported.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from caselens_data_gen.common import FactLocation, GeneratedFile
from caselens_data_gen.story import Story
from caselens_data_gen.story.models import PLATE_PREFIX, Camera
from caselens_data_gen.structured._shared import (
    CsvValue,
    Window,
    all_story_plates,
    iso,
    local_midnight,
    plan_for,
    random_local_time,
    rng_for,
    write_csv,
)

FILE_NAME = "plate_reader_hits.csv"
TABLE = "plate_reads"
HEADER = (
    "row_id",
    "plate",
    "plate_state",
    "read_at",
    "camera_id",
    "location_name",
    "latitude",
    "longitude",
    "confidence",
)
QUIET = timedelta(minutes=30)
HOUR_WEIGHTS = (1, 1, 1, 1, 1, 2, 4, 7, 9, 7, 6, 6, 6, 6, 6, 7, 8, 9, 8, 6, 4, 3, 2, 1)
NOISE_STATES = ("CA",) * 8 + ("OR", "WA", "NV")


@dataclass(frozen=True)
class LprConfig:
    first_day: date
    total_rows: int
    vehicles: int


CONFIG: dict[str, LprConfig] = {
    "riverside_lot": LprConfig(date(2026, 3, 13), total_rows=300, vehicles=150),
    "harbor_street": LprConfig(date(2026, 3, 21), total_rows=40, vehicles=30),
}


@dataclass(frozen=True)
class ReadRow:
    plate: str
    plate_state: str
    read_at: datetime
    camera: Camera
    confidence: float
    story_id: str | None = None
    event_id: str | None = None
    note: str = ""

    def values(self, row_id: int) -> list[CsvValue]:
        return [
            row_id,
            self.plate,
            self.plate_state,
            iso(self.read_at),
            self.camera.id,
            self.camera.name,
            self.camera.latitude,
            self.camera.longitude,
            f"{self.confidence:.2f}",
        ]


def _config(story: Story) -> LprConfig:
    if story.case_slug in CONFIG:
        return CONFIG[story.case_slug]
    first = min(r.read_at for r in story.plate_reads).date() - timedelta(days=1)
    return LprConfig(first, total_rows=max(40, 4 * len(story.plate_reads)), vehicles=30)


def _far_from(plate: str, others: frozenset[str]) -> bool:
    """True when ``plate`` differs from every other plate in at least two positions."""
    for other in others:
        if len(other) != len(plate):
            continue
        if sum(a != b for a, b in zip(plate, other, strict=True)) < 2:
            return False
    return True


def _noise_plates(rng: random.Random, count: int) -> list[str]:
    story_plates = all_story_plates()
    chosen: list[str] = []
    while len(chosen) < count:
        plate = PLATE_PREFIX + f"{rng.randrange(10000):04d}"
        if plate not in chosen and _far_from(plate, story_plates):
            chosen.append(plate)
    return chosen


def build_rows(story: Story) -> list[ReadRow]:
    cfg = _config(story)
    rng = rng_for(story, TABLE)
    cameras = {c.id: c for c in story.cameras}
    story_rows = [
        ReadRow(
            plate=r.plate,
            plate_state=r.plate_state,
            read_at=r.read_at,
            camera=cameras[r.camera_id],
            confidence=r.confidence,
            story_id=r.id,
            event_id=r.event_id,
            note=r.note,
        )
        for r in story.plate_reads
    ]
    alprs = [c for c in story.cameras if c.kind == "alpr"]
    quiet: dict[str, Window] = {}
    for cam in alprs:
        linked = [r.read_at for r in story.plate_reads if r.camera_id == cam.id and r.event_id]
        if linked:
            quiet[cam.id] = Window(min(linked) - QUIET, max(linked) + QUIET)

    plan = plan_for(story, "plate_reader_hits")
    window = Window(local_midnight(story, cfg.first_day), plan.recorded_at)
    plates = _noise_plates(rng, cfg.vehicles)
    states = {p: rng.choice(NOISE_STATES) for p in plates}
    # Commuters recur; most plates pass once or twice.
    weights = [1.0 / (rank + 1) ** 0.6 for rank in range(len(plates))]
    noise: list[ReadRow] = []
    while len(noise) < cfg.total_rows - len(story_rows):
        cam = rng.choice(alprs)
        read_at = random_local_time(rng, story, window, HOUR_WEIGHTS)
        if cam.id in quiet and quiet[cam.id].contains(read_at):
            continue
        plate = rng.choices(plates, weights=weights)[0]
        confidence = rng.uniform(0.62, 0.86) if rng.random() < 0.1 else rng.uniform(0.86, 0.99)
        noise.append(ReadRow(plate, states[plate], read_at, cam, round(confidence, 2)))
    return sorted(story_rows + noise, key=lambda r: r.read_at)


def generate(story: Story, csv_dir: Path) -> GeneratedFile:
    plan = plan_for(story, "plate_reader_hits")
    rows = build_rows(story)
    path = csv_dir / FILE_NAME
    write_csv(path, HEADER, [row.values(i) for i, row in enumerate(rows, start=1)])
    facts: list[FactLocation] = []
    for row_id, row in enumerate(rows, start=1):
        if row.story_id is None:
            continue
        locator = {"kind": "record", "table": TABLE, "row_id": row_id}
        facts.append(FactLocation(row.story_id, dict(locator), row.note))
        if row.event_id is not None:
            facts.append(FactLocation(row.event_id, dict(locator), f"plate read {row.story_id}"))
    return GeneratedFile(
        path=path,
        evidence_type="csv",
        description=plan.title,
        facts=facts,
        recorded_at=iso(plan.recorded_at),
    )
