"""SYNTHETIC carrier call detail records: story calls embedded among deterministic noise.

Noise rules (so planted answers never change):
* every noise row pairs one subpoenaed story phone with a non-story fictional number, so noise
  never adds a call between two story phones;
* phones shared across stories (the cross-case link) never appear in noise;
* no noise on a story phone within ``QUIET`` of any story call on that phone, and no noise
  between midnight and 07:00 local time.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from caselens_data_gen.common import FactLocation, GeneratedFile
from caselens_data_gen.story import Story
from caselens_data_gen.structured._shared import (
    CsvValue,
    Window,
    all_story_phone_numbers,
    iso,
    local_midnight,
    plan_for,
    random_local_time,
    rng_for,
    shared_phone_numbers,
    write_csv,
)

FILE_NAME = "call_detail_records.csv"
TABLE = "call_records"
HEADER = (
    "row_id",
    "caller_number",
    "callee_number",
    "started_at",
    "duration_s",
    "call_type",
    "cell_tower_id",
)
QUIET = timedelta(hours=3)
NOISE_AREA_CODES = ("360", "564", "206", "253", "425", "509", "503", "971")
EXTRA_TOWERS = ("TWR-PC-NORTH-01", "TWR-PC-MILL-05")
# Calls per hour of day; zero overnight keeps the incident nights uncluttered.
HOUR_WEIGHTS = (0, 0, 0, 0, 0, 0, 0, 2, 4, 5, 5, 5, 6, 6, 5, 5, 6, 7, 8, 8, 7, 5, 3, 1)


@dataclass(frozen=True)
class CdrConfig:
    first_day: date
    total_rows: int
    contacts: int


CONFIG: dict[str, CdrConfig] = {
    "riverside_lot": CdrConfig(date(2026, 2, 28), total_rows=500, contacts=72),
    "harbor_street": CdrConfig(date(2026, 3, 15), total_rows=60, contacts=14),
}


@dataclass(frozen=True)
class CallRow:
    caller: str
    callee: str
    started_at: datetime
    duration_s: int
    call_type: str
    tower: str | None
    story_id: str | None = None
    event_id: str | None = None
    note: str = ""

    def values(self, row_id: int) -> list[CsvValue]:
        return [
            row_id,
            self.caller,
            self.callee,
            iso(self.started_at),
            self.duration_s,
            self.call_type,
            self.tower,
        ]


def _config(story: Story) -> CdrConfig:
    if story.case_slug in CONFIG:
        return CONFIG[story.case_slug]
    first = min(c.started_at for c in story.calls).date() - timedelta(days=2)
    return CdrConfig(first, total_rows=max(60, 4 * len(story.calls)), contacts=14)


def _noise_contacts(rng: random.Random, count: int, reserved: frozenset[str]) -> list[str]:
    pool = [
        f"+1{area}55501{line:02d}"
        for area in NOISE_AREA_CODES
        for line in range(100)
        if f"+1{area}55501{line:02d}" not in reserved
    ]
    return rng.sample(pool, count)


def build_rows(story: Story) -> list[CallRow]:
    cfg = _config(story)
    rng = rng_for(story, TABLE)
    number = {p.id: p.number for p in story.phones}
    story_rows = [
        CallRow(
            caller=number[c.caller_phone_id],
            callee=number[c.callee_phone_id],
            started_at=c.started_at,
            duration_s=c.duration_s,
            call_type=c.call_type,
            tower=c.cell_tower_id,
            story_id=c.id,
            event_id=c.event_id,
            note=c.note,
        )
        for c in story.calls
    ]

    reserved = frozenset().union(*all_story_phone_numbers().values())
    contacts = _noise_contacts(rng, cfg.contacts, reserved)
    excluded = shared_phone_numbers()
    involved = {r.caller for r in story_rows} | {r.callee for r in story_rows}
    subjects = sorted(involved - excluded)
    story_times = {
        s: [r.started_at for r in story_rows if s in (r.caller, r.callee)] for s in subjects
    }
    # A subject's noise uses the towers its own story calls went through, plus one extra.
    towers: dict[str, list[str]] = {}
    for index, subject in enumerate(subjects):
        own = [r.tower for r in story_rows if r.caller == subject and r.tower]
        towers[subject] = [*sorted(set(own)), EXTRA_TOWERS[index % len(EXTRA_TOWERS)]]
    # Each subject talks mostly to a handful of regulars (Zipf-like weights).
    favourites = {s: rng.sample(contacts, len(contacts)) for s in subjects}
    contact_weights = [1.0 / (rank + 1) for rank in range(len(contacts))]

    plan = plan_for(story, "call_detail_records")
    window = Window(
        local_midnight(story, cfg.first_day),
        min(
            local_midnight(story, max(r.started_at for r in story_rows).date()) + timedelta(days=1),
            plan.recorded_at,
        ),
    )
    noise: list[CallRow] = []
    while len(noise) < cfg.total_rows - len(story_rows):
        subject = rng.choice(subjects)
        started = random_local_time(rng, story, window, HOUR_WEIGHTS)
        if any(abs(started - t) < QUIET for t in story_times[subject]):
            continue
        other = rng.choices(favourites[subject], weights=contact_weights)[0]
        call_type = "sms" if rng.random() < 0.3 else "voice"
        if call_type == "sms":
            duration = 0
        elif rng.random() < 0.08:
            duration = 0  # unanswered
        else:
            duration = min(3600, max(4, int(rng.lognormvariate(4.3, 1.0))))
        outgoing = rng.random() < 0.5
        noise.append(
            CallRow(
                caller=subject if outgoing else other,
                callee=other if outgoing else subject,
                started_at=started,
                duration_s=duration,
                call_type=call_type,
                tower=rng.choice(towers[subject]),
            )
        )
    # Stable sort: story rows win exact-time ties, so row order is deterministic.
    return sorted(story_rows + noise, key=lambda r: r.started_at)


def generate(story: Story, csv_dir: Path) -> GeneratedFile:
    plan = plan_for(story, "call_detail_records")
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
            facts.append(FactLocation(row.event_id, dict(locator), f"call {row.story_id}"))
    return GeneratedFile(
        path=path,
        evidence_type="csv",
        description=plan.title,
        facts=facts,
        recorded_at=iso(plan.recorded_at),
    )
