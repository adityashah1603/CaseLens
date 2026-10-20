"""Deterministic extraction: phone numbers, license plates, calendar dates and clock times.

Identifiers found here are exact (confidence 1.0) except generic plate-shaped tokens, which may be
something else (an item or badge number) and are written with a low confidence for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time

from caselens_workers.resolve import normalize_phone, normalize_plate

SYNTHETIC_PLATE_CONFIDENCE = 1.0
GENERIC_PLATE_CONFIDENCE = 0.5

_PHONE = re.compile(
    r"(?<![\w+])(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?![\w])"
    r"|(?<![\w+])\+1\d{10}(?!\w)"
)
_SYN_PLATE = re.compile(r"\bSYN[\s\-]?\d{4}\b")
_GENERIC_PLATE = re.compile(r"\b[A-Z]{2,3}[\s\-]?\d{3,4}\b")

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ]
    )
    for m in names
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.IGNORECASE
)
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b", re.IGNORECASE
)
_CLOCK = re.compile(
    r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([ap])\.?\s?m\b\.?)?(?![\d:])", re.IGNORECASE
)
_ISO_DATETIME = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(?:Z|[+\-]\d{2}:?\d{2})?"
)
_HOUR_MERIDIEM = re.compile(r"(?<![\d:])(\d{1,2})\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)


@dataclass(frozen=True)
class Match:
    surface: str
    value: str  # normalized identifier (E.164 phone or plate)
    confidence: float


def find_phones(text: str) -> list[Match]:
    out = []
    for m in _PHONE.finditer(text):
        e164 = normalize_phone(m.group(0))
        if e164 is not None:
            out.append(Match(m.group(0), e164, 1.0))
    return out


def find_plates(text: str) -> list[Match]:
    out: list[Match] = []
    taken: list[tuple[int, int]] = []
    for m in _SYN_PLATE.finditer(text):
        plate = normalize_plate(m.group(0))
        if plate is not None:
            out.append(Match(m.group(0), plate, SYNTHETIC_PLATE_CONFIDENCE))
            taken.append(m.span())
    for m in _GENERIC_PLATE.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        plate = normalize_plate(m.group(0))
        if plate is not None:
            out.append(Match(m.group(0), plate, GENERIC_PLATE_CONFIDENCE))
    return out


@dataclass(frozen=True)
class PartialDate:
    month: int
    day: int
    year: int | None

    def with_year(self, fallback: int | None) -> date | None:
        year = self.year if self.year is not None else fallback
        if year is None:
            return None
        try:
            return date(year, self.month, self.day)
        except ValueError:
            return None


def find_dates(text: str) -> list[PartialDate]:
    """Calendar dates in order of appearance; the year may be missing ("March 14")."""
    found: list[tuple[int, PartialDate]] = []
    spans: list[tuple[int, int]] = []

    def add(start: int, end: int, month: int, day: int, year: int | None) -> None:
        if any(s < end and start < e for s, e in spans):
            return
        if 1 <= month <= 12 and 1 <= day <= 31:
            spans.append((start, end))
            found.append((start, PartialDate(month, day, year)))

    for m in _ISO_DATE.finditer(text):
        add(m.start(), m.end(), int(m.group(2)), int(m.group(3)), int(m.group(1)))
    for m in _US_DATE.finditer(text):
        year = int(m.group(3))
        add(
            m.start(),
            m.end(),
            int(m.group(1)),
            int(m.group(2)),
            year + 2000 if year < 100 else year,
        )
    for m in _MONTH_DAY.finditer(text):
        year_s = m.group(3)
        month = _MONTHS[m.group(1).lower()]
        add(m.start(), m.end(), month, int(m.group(2)), int(year_s) if year_s else None)
    for m in _DAY_MONTH.finditer(text):
        year_s = m.group(3)
        month = _MONTHS[m.group(2).lower()]
        add(m.start(), m.end(), month, int(m.group(1)), int(year_s) if year_s else None)
    return [d for _, d in sorted(found, key=lambda item: item[0])]


def _to_24h(hour: int, meridiem: str | None) -> int | None:
    if meridiem is None:
        return hour if hour <= 23 else None
    if not 1 <= hour <= 12:
        return None
    if meridiem.lower() == "a":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def find_times(text: str) -> list[time]:
    """Clock times in order ("22:41:05", "10:15 PM", "10 p.m."). Without AM/PM, 24-hour."""
    found: list[tuple[int, time]] = []
    spans: list[tuple[int, int]] = []
    for m in _ISO_DATETIME.finditer(text):
        iso_h, iso_m, iso_s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if iso_h <= 23 and iso_m <= 59 and iso_s <= 59:
            spans.append(m.span())
            found.append((m.start(), time(iso_h, iso_m, iso_s)))
    for m in _CLOCK.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        hour = _to_24h(int(m.group(1)), m.group(4))
        minute, second = int(m.group(2)), int(m.group(3) or 0)
        if hour is not None and minute <= 59 and second <= 59:
            spans.append(m.span())
            found.append((m.start(), time(hour, minute, second)))
    for m in _HOUR_MERIDIEM.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        hour = _to_24h(int(m.group(1)), m.group(2))
        if hour is not None:
            found.append((m.start(), time(hour)))
    return [t for _, t in sorted(found, key=lambda item: item[0])]
