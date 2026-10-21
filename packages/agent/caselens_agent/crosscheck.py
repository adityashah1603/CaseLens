"""A cheap deterministic pre-check for contradictions between observed sources.

Before each model turn the loop lists, per source, the values that witnesses and records most
often disagree on: clock times, vehicle colors, vehicle makes, and occupant / alibi phrases
("by myself", "two men", "never got out"). Only categories where the sources give two or more
different values are shown. It is a hint, not a finding: the model decides whether two values
describe the same event and, if so, surfaces the contradiction in `conflicts[]` (and calls
flag_conflict). The values are copied from evidence text, so the note is rendered inside an
`evidence_block` like every other observation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from caselens_agent.router import evidence_block
from caselens_agent.sources import ObservedSource, SourceLedger

_TIME = re.compile(
    r"(?<![\d:])(?P<h>\d{1,2})(?::(?P<m>\d{2}))(?::(?P<s>\d{2}))?(?:\s*(?P<mer>[ap])\.?\s?m\b\.?)?"
    r"|(?<![\d:])(?P<h2>\d{1,2})\s*(?P<mer2>[ap])\.?\s?m\b\.?",
    re.IGNORECASE,
)
_COLORS = (
    "gray|grey|silver|blue|dark blue|light blue|white|black|red|maroon|green|tan|beige|brown|"
    "gold|yellow|orange"
)
_VEHICLE_NOUNS = (
    "car|sedan|coupe|hatchback|truck|pickup|suv|van|vehicle|four-door|two-door|civic|camry|"
    "corolla|accord|f-150|motorcycle"
)
_COLOR = re.compile(
    rf"\b(?P<color>{_COLORS})(?:\s+or\s+(?:{_COLORS}))?(?:[\s,-]+[\w-]+){{0,3}}?\s+"
    rf"(?:{_VEHICLE_NOUNS})\b",
    re.IGNORECASE,
)
_MAKE = re.compile(
    r"\b(honda|toyota|ford|chevrolet|chevy|nissan|hyundai|kia|subaru|mazda|dodge|jeep|bmw|"
    r"volkswagen|vw|tesla|lexus|acura)\b",
    re.IGNORECASE,
)
_OCCUPANTS = re.compile(
    r"\b(by (?:my|him|her)self|alone|on (?:my|his|her) own|nobody (?:else|with me)|"
    r"(?:one|two|three|2|3) (?:men|man|people|persons|occupants|guys)|a passenger|"
    r"the passenger|never (?:even )?got out|got out|stayed in the car|left before)\b",
    re.IGNORECASE,
)
MAX_SOURCES = 10
MAX_VALUES = 4


def _clock(m: re.Match[str]) -> str:
    if m.group("h2") is not None:
        h, minute, sec, mer = int(m.group("h2")), 0, None, m.group("mer2")
    else:
        h, minute = int(m.group("h")), int(m.group("m"))
        sec = m.group("s")
        mer = m.group("mer")
    if mer:
        mer = mer.lower()
        if not 1 <= h <= 12:
            return ""
        h = (h % 12) + (12 if mer == "p" else 0)
    elif h > 23:
        return ""
    if minute > 59:
        return ""
    text = f"{h:02d}:{minute:02d}" + (f":{sec}" if sec else "")
    # A 1-12 hour with no am/pm is ambiguous; keep it marked so it is not read as morning.
    return text if mer or h > 12 or h == 0 else f"{text} (am/pm not stated)"


def _values(source: ObservedSource) -> dict[str, list[str]]:
    text = " ".join(source.texts)
    found: dict[str, list[str]] = {
        "clock times": [v for v in (_clock(m) for m in _TIME.finditer(text)) if v],
        "vehicle colors": [m.group(0).lower() for m in _COLOR.finditer(text)],
        "vehicle makes": [m.group(1).capitalize() for m in _MAKE.finditer(text)],
        "occupants / alibi": [m.group(1).lower() for m in _OCCUPANTS.finditer(text)],
    }
    return {k: list(dict.fromkeys(v))[:MAX_VALUES] for k, v in found.items() if v}


def _key(category: str, value: str) -> str:
    if category == "vehicle colors":
        return value.split()[0].replace("grey", "gray")
    if category == "clock times":
        return value[:5]
    return value


def cross_check(sources: Iterable[ObservedSource]) -> str | None:
    """The cross-check note for the given sources, or None when nothing differs."""
    per_source: list[tuple[str, dict[str, list[str]]]] = []
    for s in sources:
        values = _values(s)
        if values:
            per_source.append((s.handle, values))
    lines: list[str] = []
    for category in ("clock times", "vehicle colors", "vehicle makes", "occupants / alibi"):
        rows = [(h, v[category]) for h, v in per_source if category in v]
        distinct = {_key(category, x) for _, vals in rows for x in vals}
        if len(rows) < 2 or len(distinct) < 2:
            continue
        shown = rows[:MAX_SOURCES]
        lines.append(f"{category}: " + " | ".join(f"{h}: {', '.join(v)}" for h, v in shown))
    return "\n".join(lines) if lines else None


def cross_check_note(ledger: SourceLedger) -> str | None:
    """`cross_check` over the sources observed so far, wrapped for the model. Prose sources
    (statements, reports, transcripts, video summaries) come first; records rarely disagree, so
    only a few are included."""
    prose = [s for s in ledger.sources() if s.ref.source_type == "chunk"]
    records = [s for s in ledger.sources() if s.ref.source_type == "record"]
    body = cross_check([*prose, *records[:3]])
    if body is None:
        return None
    return (
        "Cross-check (automatic and heuristic; verify against the sources): values that differ "
        "between the observed sources. Where two sources give different values for the SAME "
        "event or object relevant to the question (a time, a vehicle's color or make, who was in "
        "it, an alibi), that is a contradiction: put it in conflicts[] with both sides cited and "
        "call flag_conflict. Different values for different events are not conflicts.\n"
        + evidence_block("cross-check of observed sources", body)
    )
