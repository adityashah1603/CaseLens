"""Wall-clock time resolution for extracted events.

Order of preference:
1. an ISO 8601 time the model resolved (naive values are read in the case timezone);
2. an explicit date plus a clock time in the event's time text;
3. a clock time combined with the date of the source chunk's `t_start`, else the evidence's
   `recorded_at`, else the only calendar date written in the source chunks;
4. for audio/video narrated live ("happening now"), the source chunk's own `t_start`.
A bare date (no time of day) resolves to local midnight and is always flagged for review.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from caselens_workers.extraction.patterns import find_dates, find_times

DEFAULT_CASE_TIMEZONE = "America/Los_Angeles"


def case_timezone() -> ZoneInfo:
    """The case timezone: env CASELENS_CASE_TIMEZONE, default America/Los_Angeles."""
    return ZoneInfo(os.environ.get("CASELENS_CASE_TIMEZONE") or DEFAULT_CASE_TIMEZONE)


@dataclass(frozen=True)
class ResolvedTime:
    t_start: datetime
    t_end: datetime | None
    needs_review: bool  # the time is a guess (e.g. date only)
    basis: str  # iso | text | reference | live | date_only


def _parse_iso(value: str | None, tz: ZoneInfo) -> tuple[datetime, bool] | None:
    """(aware datetime, has_time_of_day) or None."""
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time(), tz), False
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed, True


def _combine(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tz)


def _end_after(start: datetime, end: datetime | None) -> datetime | None:
    if end is None:
        return None
    if end < start and end + timedelta(days=1) - start <= timedelta(hours=12):
        return end + timedelta(days=1)  # "23:50-00:10" crosses midnight
    return end if end >= start else None


def resolve_event_time(
    *,
    iso_start: str | None,
    iso_end: str | None,
    time_text: str | None,
    happening_now: bool,
    chunk_t_starts: Sequence[datetime | None],
    chunk_t_ends: Sequence[datetime | None],
    chunk_texts: Sequence[str],
    recorded_at: datetime | None,
    tz: ZoneInfo | None = None,
) -> ResolvedTime | None:
    tz = tz or case_timezone()
    start_iso = _parse_iso(iso_start, tz)
    if start_iso is not None and start_iso[1]:
        end_iso = _parse_iso(iso_end, tz)
        end = end_iso[0] if end_iso is not None and end_iso[1] else None
        return ResolvedTime(start_iso[0], _end_after(start_iso[0], end), False, "iso")

    chunk_start = next((t for t in chunk_t_starts if t is not None), None)
    chunk_end = next((t for t in reversed(chunk_t_ends) if t is not None), None)
    anchor = chunk_start or recorded_at
    year_hint = anchor.astimezone(tz).year if anchor is not None else None

    text_dates = find_dates(time_text or "")
    clocks = find_times(time_text or "")
    explicit = next(
        (d for d in (p.with_year(year_hint) for p in text_dates) if d is not None), None
    )
    reference: date | None = None
    if chunk_start is not None:
        reference = chunk_start.astimezone(tz).date()
    elif recorded_at is not None:
        reference = recorded_at.astimezone(tz).date()
    else:
        written = {d for d in (p.with_year(None) for t in chunk_texts for p in find_dates(t)) if d}
        if len(written) == 1:
            reference = next(iter(written))

    if clocks:
        day = explicit or reference
        if day is not None:
            start = _combine(day, clocks[0], tz)
            end = _combine(day, clocks[1], tz) if len(clocks) > 1 else None
            basis = "text" if explicit is not None else "reference"
            return ResolvedTime(start, _end_after(start, end), False, basis)

    if happening_now and chunk_start is not None:
        return ResolvedTime(chunk_start, _end_after(chunk_start, chunk_end), False, "live")

    day_only = explicit or (start_iso[0].date() if start_iso is not None else None)
    if day_only is not None:
        return ResolvedTime(_combine(day_only, time(), tz), None, True, "date_only")
    return None
