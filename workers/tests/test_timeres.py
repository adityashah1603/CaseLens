from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from caselens_workers.extraction.timeres import resolve_event_time

TZ = ZoneInfo("America/Los_Angeles")


def _resolve(iso: str | None, text: str, recorded: datetime | None) -> datetime:
    out = resolve_event_time(
        iso_start=iso,
        iso_end=None,
        time_text=text,
        happening_now=False,
        chunk_t_starts=[None],
        chunk_t_ends=[None],
        chunk_texts=["SYNTHETIC statement"],
        recorded_at=recorded,
        tz=TZ,
    )
    assert out is not None
    return out.t_start.astimezone(TZ)


def test_unmarked_clock_time_in_a_statement_is_placed_before_the_statement() -> None:
    taken = datetime(2026, 3, 14, 23, 40, tzinfo=TZ)
    assert _resolve("2026-03-14T10:41:00", "at 10:41", taken).hour == 22
    assert _resolve(None, "about 10:48", taken).hour == 22


def test_marked_or_24h_times_are_left_alone() -> None:
    taken = datetime(2026, 3, 15, 16, 0, tzinfo=TZ)
    assert _resolve("2026-03-14T22:15:00", "10:15 at night", taken).hour == 22
    assert _resolve("2026-03-15T10:15:00", "10:15 a.m.", taken).hour == 10
    assert _resolve("2026-03-14T22:41:00", "22:41", taken).hour == 22


def test_latest_candidate_not_after_the_document() -> None:
    # Statement taken at 16:00 on 03-15 says "10:15": both 10:15 and 22:15 on 03-14 are earlier
    # than 03-15 16:00 only for the same-day 10:15 AM candidate from the model's ISO date.
    taken = datetime(2026, 3, 15, 16, 0, tzinfo=TZ)
    assert _resolve("2026-03-15T10:15:00", "10:15", taken) == datetime(
        2026, 3, 15, 10, 15, tzinfo=TZ
    )
    assert _resolve("2026-03-14T10:15:00", "10:15", taken) == datetime(
        2026, 3, 14, 22, 15, tzinfo=TZ
    )


def test_meridiem_markers_are_detected() -> None:
    from caselens_workers.extraction.timeres import _MERIDIEM

    assert (
        _MERIDIEM.search("10:15 p.m.")
        and _MERIDIEM.search("at 10 PM")
        and _MERIDIEM.search("night")
    )
    assert not _MERIDIEM.search("at 10:41") and not _MERIDIEM.search("Campbell")
