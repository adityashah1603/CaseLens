"""Helpers shared by the SYNTHETIC structured-data (CSV) generators."""

from __future__ import annotations

import csv
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from caselens_data_gen.common import SYNTHETIC_NOTICE
from caselens_data_gen.story import STORY_SLUGS, Story, load_story
from caselens_data_gen.story.models import EvidencePlanItem

CsvValue = str | int | float | None

COMMENT_PREFIX = "# "
"""Line 1 of every CSV is ``# <SYNTHETIC_NOTICE>``. Loaders skip every line that starts with '#'."""


@dataclass(frozen=True)
class Window:
    """A half-open time window [start, end)."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def rng_for(story: Story, table: str) -> random.Random:
    """A deterministic RNG per (case, table); string seeds are hashed stably by ``random``."""
    return random.Random(f"caselens-structured:{story.case_slug}:{table}")  # noqa: S311


def plan_for(story: Story, kind: str) -> EvidencePlanItem:
    matches = [p for p in story.evidence_plan if p.kind == kind]
    if len(matches) != 1:
        raise ValueError(f"{story.case_slug}: expected one {kind!r} evidence plan item")
    return matches[0]


def local_midnight(story: Story, day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(story.timezone))


def random_local_time(
    rng: random.Random, story: Story, window: Window, hour_weights: Sequence[int]
) -> datetime:
    """A random wall-clock time in ``window`` (local zone), hour drawn from ``hour_weights``."""
    zone = ZoneInfo(story.timezone)
    first = window.start.astimezone(zone).date()
    days = (window.end.astimezone(zone).date() - first).days + 1
    while True:
        day = first + timedelta(days=rng.randrange(days))
        hour = rng.choices(range(24), weights=hour_weights)[0]
        moment = datetime.combine(
            day, time(hour, rng.randrange(60), rng.randrange(60)), tzinfo=zone
        )
        if window.contains(moment):
            return moment


@cache
def all_story_phone_numbers() -> dict[str, frozenset[str]]:
    """Every story phone number, keyed by case slug (used to keep noise off story numbers)."""
    return {slug: frozenset(p.number for p in load_story(slug).phones) for slug in STORY_SLUGS}


@cache
def all_story_plates() -> frozenset[str]:
    plates: set[str] = set()
    for slug in STORY_SLUGS:
        story = load_story(slug)
        plates.update(v.plate for v in story.vehicles)
        plates.update(r.plate for r in story.plate_reads)
    return frozenset(plates)


def shared_phone_numbers() -> frozenset[str]:
    """Numbers that appear in more than one story (the deliberate cross-case link)."""
    seen: dict[str, int] = {}
    for numbers in all_story_phone_numbers().values():
        for number in numbers:
            seen[number] = seen.get(number, 0) + 1
    return frozenset(n for n, count in seen.items() if count > 1)


def iso(moment: datetime) -> str:
    """ISO 8601 with an explicit UTC offset, second precision."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.replace(microsecond=0).isoformat()


def write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[CsvValue]]) -> None:
    """Write the SYNTHETIC comment line, the header, then ``rows`` (``row_id`` is column 1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(COMMENT_PREFIX + SYNTHETIC_NOTICE + "\n")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
