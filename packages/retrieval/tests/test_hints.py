"""Query hints for the record lane: identifiers, record tables and local time windows.
Pure functions; all examples SYNTHETIC."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from caselens_retrieval.hints import (
    CALL_TABLE,
    CUSTODY_TABLE,
    PLATE_TABLE,
    find_item_ids,
    find_phones,
    find_plates,
    owner_named_in,
    parse_hints,
    plate_of,
    time_window,
    vehicle_names_in,
)
from caselens_retrieval.search import described_slots, lane_slots

LA = ZoneInfo("America/Los_Angeles")


def w(text: str) -> tuple[str, str] | None:
    window = time_window(text, LA, 2026)
    return None if window is None else (window[0].isoformat(), window[1].isoformat())


def test_identifiers() -> None:
    text = "SYNTHETIC: +13605550117, (360) 555-0142, plate SYN 4821? SYN482I, ITEM-007, 2026, 10PM"
    assert find_phones(text) == ["+13605550117", "+13605550142"]
    assert find_plates(text) == ["SYN482I"]
    assert find_plates("plate SYN4821 at C-7 by ET-17") == ["SYN4821"]
    assert find_item_ids(text) == ["ITEM-007"]


def test_time_windows() -> None:
    assert w("Who was in the gray sedan on March 14?") == (
        "2026-03-14T00:00:00-07:00",
        "2026-03-15T00:00:00-07:00",
    )
    assert w("between 10 and 11 PM on March 14") == (
        "2026-03-14T22:00:00-07:00",
        "2026-03-14T23:00:00-07:00",
    )
    assert w("between 10:30 and 11 PM on March 14") == (
        "2026-03-14T22:30:00-07:00",
        "2026-03-14T23:00:00-07:00",
    )
    assert w("between 22:20 and 22:40 on March 14") == (
        "2026-03-14T22:20:00-07:00",
        "2026-03-14T22:40:00-07:00",
    )
    assert w("from 9 PM on March 14 to 2 AM on March 15") == (
        "2026-03-14T21:00:00-07:00",
        "2026-03-15T02:00:00-07:00",
    )
    assert w("on March 14 and 15") == ("2026-03-14T00:00:00-07:00", "2026-03-16T00:00:00-07:00")
    assert w("the evening of March 14") == (
        "2026-03-14T18:00:00-07:00",
        "2026-03-15T00:00:00-07:00",
    )
    assert w("the night of March 14") == ("2026-03-14T18:00:00-07:00", "2026-03-15T06:00:00-07:00")
    assert w("at 12:58 AM on March 15") == (
        "2026-03-15T00:28:00-07:00",
        "2026-03-15T01:28:00-07:00",
    )
    assert w("after 10 PM on 2026-03-14") == (
        "2026-03-14T22:00:00-07:00",
        "2026-03-15T04:00:00-07:00",
    )
    # A clock time with no date, or no date at all, is no window.
    assert w("left before 10:40") is None
    assert w("in the week before the homicide") is None
    assert time_window("March 14", LA, 2026) == (
        datetime(2026, 3, 14, tzinfo=LA),
        datetime(2026, 3, 15, tzinfo=LA),
    )


def test_tables_from_words_and_identifiers() -> None:
    def tables(q: str, derived: tuple[str, ...] = ()) -> tuple[str, ...]:
        return parse_hints(q, LA, 2026, derived_plates=derived).tables

    assert tables("List every call between +13605550117 and +13605550142") == (CALL_TABLE,)
    assert tables("Show every ALPR read of SYN4821") == (PLATE_TABLE,)
    assert tables("What is the chain of custody for the casing?") == (CUSTODY_TABLE,)
    assert tables("Who handled ITEM-001?") == (CUSTODY_TABLE,)
    # A described vehicle implies plate reads only for a movement question.
    assert tables("Where was the gray sedan?", ("SYN4821",)) == (PLATE_TABLE,)
    assert tables("Who was in the gray sedan?", ("SYN4821",)) == ()


def test_vehicle_names_owners_and_plates() -> None:
    assert vehicle_names_in("Trace the gray Civic home", ["SYN4821", "gray civic", "car"])
    assert not vehicle_names_in("the car left", ["car"])  # too short to be specific
    assert not vehicle_names_in("a gray civics fan", ["gray civic"])
    assert owner_named_in("Lay out Wes Darnell's movements", "2012 Camry registered to Wes Darnell")
    assert not owner_named_in("Where did Hale go?", "2012 Camry registered to Wes Darnell")
    assert plate_of({"plate": "SYN4821 (CA)"}, "desc:gray honda civic") == "SYN4821"
    assert plate_of({"plate": "UNKNOWN"}, "UNKNOWN") is None
    assert plate_of({"plate": "SYN48"}, "SYN48") is None  # a partial read is not a plate


def test_lane_slots_leave_room_for_ranked_results() -> None:
    assert [lane_slots(k) for k in (1, 8, 10, 20)] == [1, 6, 7, 14]
    assert [described_slots(k) for k in (1, 8, 10)] == [1, 3, 4]
