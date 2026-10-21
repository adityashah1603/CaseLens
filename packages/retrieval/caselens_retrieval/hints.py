"""Structured hints in a search query: phone numbers, plates, item tags, record tables and a
local time window. Plain regex heuristics over the query text (no model, nothing case-specific).

The record lane in `search.py` uses them to pull the matching structured-record chunks (call
detail rows, plate reads, evidence-log rows) into the results. Those rows read like
"Plate read row 250: plate SYN4821 (CA) read at 2026-03-14 22:24:37-07:00 by camera ...", so
neither the embedding nor the cross-encoder ranks them well against a natural-language question,
even when the question names the plate and the time.

Dates without a year take `default_year` (the search derives it from the case's own timed
chunks); times are wall-clock times in the case timezone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

CALL_TABLE = "call_records"
PLATE_TABLE = "plate_reads"
CUSTODY_TABLE = "custody_records"

_APOS = "'" + chr(0x2019)
_PHONE = re.compile(r"(?<![\w+])(\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\w)")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_PLATE = re.compile(r"^(?=.*\d)(?=.*[A-Z])[A-Z0-9]{5,8}$")
_ITEM = re.compile(r"\bITEM-\d{1,5}\b", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_MONTH_RE = (
    r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_NOT_TIME_AFTER = r"(?!\s*(?:[ap]\.?\s?m\b|:|\d|o'?clock))"
_DATE = re.compile(
    rf"\b{_MONTH_RE}\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b"
    rf"(?:\s*(?:and|&|-|to|through|or)\s*(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?\b{_NOT_TIME_AFTER})?"
    r"(?:,?\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<mon>\d{2})-(?P<day>\d{2})\b")
_SLASH_DATE = re.compile(r"\b(?P<mon>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b")
_TIME = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?(?::(?P<s>\d{2}))?"
    r"(?:\s*(?P<mer>[ap])\.?\s?m\b\.?)?",
    re.IGNORECASE,
)
_RANGE_START = re.compile(r"\b(?:between|from)\s*$", re.IGNORECASE)
_RANGE_JOIN = re.compile(r"^\s*(?:and|to|-|until|till)\s*$", re.IGNORECASE)
_AFTER = re.compile(r"\b(?:after|since)\s*$", re.IGNORECASE)
_BEFORE = re.compile(r"\b(?:before|until|by)\s*$", re.IGNORECASE)

# Parts of a day, as (start hour, length in hours), for a date with no clock time.
_DAY_PARTS: tuple[tuple[re.Pattern[str], int, int], ...] = (
    (re.compile(r"\b(?:night|overnight|tonight)\b", re.IGNORECASE), 18, 12),
    (re.compile(r"\bevening\b", re.IGNORECASE), 18, 6),
    (re.compile(r"\bafternoon\b", re.IGNORECASE), 12, 6),
    (re.compile(r"\bmorning\b", re.IGNORECASE), 6, 6),
)

_CALL_WORDS = re.compile(
    r"\b(?:calls?|called|calling|phones?|cell|sms|texts?|texted|messages?|contacts?|dialed|"
    r"rang|voicemail)\b",
    re.IGNORECASE,
)
_PLATE_WORDS = re.compile(
    r"\b(?:plates?|alpr|plate[- ]readers?|readers?|reads?|sightings?|camera\s+read)\b",
    re.IGNORECASE,
)
_MOVE_WORDS = re.compile(
    r"\b(?:where|trace|route|movements?|went|drove|driven|left|leave|leaving|arrive[ds]?|"
    r"arriving|enter(?:ed|s)?|exit(?:ed|s)?|passed|seen|sighted|came back|got back|returned)\b",
    re.IGNORECASE,
)
_CUSTODY_WORDS = re.compile(
    r"\b(?:custody|evidence log|handled|seized|collected|logged|property room|chain of|"
    r"search of|searched|recovered)\b",
    re.IGNORECASE,
)
_SINGLE_TIME_SPAN = timedelta(minutes=30)
_OPEN_SPAN = timedelta(hours=6)


@dataclass(frozen=True)
class QueryHints:
    phones: tuple[str, ...] = ()
    plates: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    window: tuple[datetime, datetime] | None = None
    mentions_movement: bool = False

    @property
    def has_identifiers(self) -> bool:
        return bool(self.phones or self.plates or self.item_ids)


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if len(digits) == 11 and digits.startswith("1") else None


def find_phones(text: str) -> list[str]:
    found = [normalize_phone(m.group(0)) for m in _PHONE.finditer(text)]
    return list(dict.fromkeys(p for p in found if p))


def find_plates(text: str) -> list[str]:
    scrubbed = _PHONE.sub(" ", _ITEM.sub(" ", text))
    out = []
    for token in _TOKEN.findall(scrubbed):
        t = token.strip(_APOS + "-").upper()
        if _PLATE.match(t) and not t.isdigit():
            out.append(t)
    return list(dict.fromkeys(out))


def find_item_ids(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).upper() for m in _ITEM.finditer(text)))


@dataclass
class _Date:
    start: int
    end: int
    first: date
    last: date


@dataclass
class _Time:
    start: int
    end: int
    hour: int
    minute: int
    second: int
    mer: str | None
    explicit: bool  # has minutes or am/pm


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates(text: str, default_year: int) -> list[_Date]:
    out: list[_Date] = []
    for m in _DATE.finditer(text):
        year = int(m.group("year")) if m.group("year") else default_year
        month = _MONTHS[m.group("mon").lower()[:3]]
        first = _safe_date(year, month, int(m.group("day")))
        if first is None:
            continue
        last = first
        if m.group("day2"):
            second = _safe_date(year, month, int(m.group("day2")))
            if second is not None and second >= first:
                last = second
        out.append(_Date(m.start(), m.end(), first, last))
    for m in _ISO_DATE.finditer(text):
        d = _safe_date(int(m.group("year")), int(m.group("mon")), int(m.group("day")))
        if d is not None:
            out.append(_Date(m.start(), m.end(), d, d))
    for m in _SLASH_DATE.finditer(text):
        y = m.group("year")
        year = default_year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        d = _safe_date(year, int(m.group("mon")), int(m.group("day")))
        if d is not None:
            out.append(_Date(m.start(), m.end(), d, d))
    return sorted(out, key=lambda d: d.start)


def _times(text: str, taken: list[tuple[int, int]]) -> list[_Time]:
    cands: list[_Time] = []
    for m in _TIME.finditer(text):
        if any(a <= m.start() < b or a < m.end() <= b for a, b in taken):
            continue
        h = int(m.group("h"))
        minute = int(m.group("m") or 0)
        sec = int(m.group("s") or 0)
        mer = m.group("mer").lower() if m.group("mer") else None
        if h > 23 or minute > 59 or sec > 59 or (mer and not 1 <= h <= 12):
            continue
        cands.append(_Time(m.start(), m.end(), h, minute, sec, mer, bool(m.group("m") or mer)))
    # "between 10 and 11 PM": a bare number in a range with an explicit time counts too.
    for i, t in enumerate(cands):
        if t.explicit:
            continue
        nxt = cands[i + 1] if i + 1 < len(cands) else None
        if (
            nxt is not None
            and nxt.explicit
            and _RANGE_START.search(text[: t.start])
            and _RANGE_JOIN.match(text[t.end : nxt.start])
        ):
            t.explicit = True
    times = [t for t in cands if t.explicit]
    # A time with no am/pm takes its range partner's ("10:30 and 11 PM").
    for i, t in enumerate(times):
        if t.mer is None and t.hour <= 12:
            partner = times[i + 1] if i + 1 < len(times) else None
            if (
                partner is not None
                and partner.mer
                and _RANGE_JOIN.match(text[t.end : partner.start])
            ):
                t.mer = partner.mer
    return times


def _clock(t: _Time) -> time:
    h = t.hour
    if t.mer == "p" and h < 12:
        h += 12
    elif t.mer == "a" and h == 12:
        h = 0
    return time(h, t.minute, t.second)


def _bind(t: _Time, dates: list[_Date], times: list[_Time]) -> _Date:
    """The date a time belongs to: the next date with no other time in between, else the
    nearest date."""
    for d in dates:
        if d.start >= t.end and not any(t.end <= o.start < d.start for o in times if o is not t):
            return d
    return min(dates, key=lambda d: min(abs(d.start - t.end), abs(t.start - d.end)))


def time_window(text: str, tz: tzinfo, default_year: int) -> tuple[datetime, datetime] | None:
    """The local time window a query names, as aware datetimes, or None."""
    dates = _dates(text, default_year)
    if not dates:
        return None
    taken = [(d.start, d.end) for d in dates]
    taken += [(m.start(), m.end()) for m in _PHONE.finditer(text)]
    times = _times(text, taken)

    def at(d: date, clock: time) -> datetime:
        return datetime.combine(d, clock, tzinfo=tz)

    if not times:
        first, last = dates[0].first, max(d.last for d in dates)
        if first == last:
            for pattern, hour, length in _DAY_PARTS:
                if pattern.search(text):
                    start = at(first, time(hour))
                    return start, start + timedelta(hours=length)
        return at(first, time(0)), at(last + timedelta(days=1), time(0))
    if len(times) >= 2:
        a, b = times[0], times[1]
        start = at(_bind(a, dates, times).first, _clock(a))
        end = at(_bind(b, dates, times).first, _clock(b))
        if end <= start:
            end += timedelta(days=1)
        return start, end
    t = times[0]
    point = at(_bind(t, dates, times).first, _clock(t))
    before = text[: t.start]
    if _AFTER.search(before):
        return point, point + _OPEN_SPAN
    if _BEFORE.search(before):
        return point - _OPEN_SPAN, point
    return point - _SINGLE_TIME_SPAN, point + _SINGLE_TIME_SPAN


def parse_hints(
    query: str, tz: tzinfo, default_year: int, derived_plates: tuple[str, ...] = ()
) -> QueryHints:
    """Hints for `query`; `derived_plates` are plates of vehicles the query describes."""
    phones = tuple(find_phones(query))
    plates = tuple(find_plates(query))
    items = tuple(find_item_ids(query))
    tables: list[str] = []
    if phones or _CALL_WORDS.search(query):
        tables.append(CALL_TABLE)
    movement = bool(_MOVE_WORDS.search(query))
    if plates or _PLATE_WORDS.search(query) or (derived_plates and movement):
        tables.append(PLATE_TABLE)
    if items or _CUSTODY_WORDS.search(query):
        tables.append(CUSTODY_TABLE)
    return QueryHints(
        phones=phones,
        plates=plates,
        item_ids=items,
        tables=tuple(tables),
        window=time_window(query, tz, default_year),
        mentions_movement=movement,
    )


_VEHICLE_NAME_MIN = 5


def vehicle_names_in(query: str, names: list[str]) -> bool:
    """True when one of a vehicle entity's names appears as a whole phrase in the query."""
    q = " " + re.sub(r"[^a-z0-9]+", " ", query.casefold()) + " "
    for name in names:
        n = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
        if len(n) >= _VEHICLE_NAME_MIN and f" {n} " in q:
            return True
    return False


_OWNER = re.compile(
    r"\b(?:registered to|owned by|belongs to|belonging to)\s+"
    r"(?P<name>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*)"
)
_OWNER_TOKEN_MIN = 4


def owner_named_in(query: str, description: str) -> bool:
    """True when a vehicle description names its owner ("registered to Wes Darnell") and the
    query names that owner (any name part of 4+ letters, as a whole word)."""
    q = set(re.findall(r"[a-z]+", query.casefold()))
    for m in _OWNER.finditer(description):
        parts = [p for p in re.findall(r"[a-z]+", m.group("name").casefold())]
        if any(len(p) >= _OWNER_TOKEN_MIN and p in q for p in parts):
            return True
    return False


def plate_of(attributes: dict[str, object], normalized_key: str | None) -> str | None:
    """A full plate for a vehicle entity, from its `plate` attribute or normalized key."""
    for raw in (attributes.get("plate"), normalized_key):
        if isinstance(raw, str):
            for token in _TOKEN.findall(raw.upper()):
                if _PLATE.match(token) and len(token) >= 6:
                    return str(token)
    return None
