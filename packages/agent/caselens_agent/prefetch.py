"""Retrieval-first fast path: tool calls the runtime makes before the first model call.

Most questions need the same opening moves (search the evidence and the video for the question,
look up the people / plates / phones it names), and the model used to spend one or two planning
round trips choosing them. The loop runs them up front so the first model turn already sees the
observations and can often answer at once.

Questions about records also get the matching semantic-layer operation up front (`record_calls`):
the phones, plates, item tags and local time window are parsed by `caselens_retrieval.hints`,
and the returned rows are citable sources.

`salient_terms` is a plain heuristic over the question text (no model, nothing case-specific):
phone numbers, plate-like tokens, runs of capitalized words (names, places) that are not
question words, months or weekdays, and "<color> <vehicle>" descriptions.
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo

from caselens_agent.prompts import PlannedCall

_APOSTROPHES = "'" + chr(0x2019)  # ASCII and typographic
_PHONE = re.compile(r"(?<!\w)\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_TOKEN = re.compile(f"[A-Za-z0-9][A-Za-z0-9{_APOSTROPHES}-]*")
_PLATE = re.compile(r"^(?=.*\d)(?=.*[A-Z])[A-Z0-9]{4,8}$")
_VEHICLE = re.compile(
    r"\b(?:gr[ae]y|black|white|silver|red|blue|green|dark|light|tan|brown|gold|maroon|beige)"
    r"\s+(?:sedan|car|suv|truck|pickup|van|coupe|hatchback|vehicle|motorcycle)\b",
    re.IGNORECASE,
)
_POSSESSIVE = re.compile(f"[{_APOSTROPHES}]s$")
_NOT_NAMES = frozenset(
    re.findall(
        r"\w+",
        """
        who whom whose what when where why how which did does do is was were are can could would
        should will has have had list show find give tell describe summarize compare the a an in
        on at of for from to and or but if then this that these those there their i we you he she
        it they any all every each between before after during
        january february march april may june july august september october november december
        jan feb mar apr jun jul aug sep sept oct nov dec
        monday tuesday wednesday thursday friday saturday sunday
        """,
    )
)


def salient_terms(question: str, limit: int = 2) -> list[str]:
    """Up to `limit` entity names to look up for `question`, most specific first."""
    found: list[str] = []
    found += [m.group(0).strip() for m in _PHONE.finditer(question)]
    tokens = [t.strip(_APOSTROPHES + "-") for t in _TOKEN.findall(question)]
    found += [t for t in tokens if _PLATE.match(t)]
    run: list[str] = []
    for token in [*tokens, ""]:
        word = _POSSESSIVE.sub("", token)
        if word[:1].isupper() and word.casefold() not in _NOT_NAMES and not _PLATE.match(word):
            run.append(word)
            if word != token:  # a possessive ends the name: "Wes Darnell's Camry"
                found.append(" ".join(run))
                run = []
            continue
        if run:
            found.append(" ".join(run))
            run = []
    found += [m.group(0) for m in _VEHICLE.finditer(question)]
    out: list[str] = []
    seen: set[str] = set()
    for term in found:
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out[:limit]


# Without a date in the question, record operations get this wide window around the case year
# (the model narrows it when the question implies a period such as "the week before").
_WIDE_YEARS = 1
MAX_RECORD_PREFETCH = 2


def _iso(value: datetime) -> str:
    return value.isoformat()


def names_records(question: str) -> bool:
    """True when the question names a phone number, plate or evidence item tag."""
    from caselens_retrieval.hints import find_item_ids, find_phones, find_plates

    return bool(find_phones(question) or find_plates(question) or find_item_ids(question))


def record_calls(question: str, tz: tzinfo, year: int) -> list[PlannedCall]:
    """query_records calls for the phones, plates and item tags a question names, windowed by
    the local time window it names (else a wide window): calls_between for two phones,
    calls_for_phone for one, plate_sightings per plate, custody_chain per item tag."""
    from caselens_retrieval.hints import parse_hints

    hints = parse_hints(question, tz, year)
    if hints.window is not None:
        start, end = hints.window
    else:
        start = datetime(year - _WIDE_YEARS, 1, 1, tzinfo=tz)
        end = datetime(year + _WIDE_YEARS + 1, 1, 1, tzinfo=tz)
    window = {"window_start": _iso(start), "window_end": _iso(end)}
    calls: list[PlannedCall] = []
    if len(hints.phones) >= 2:
        calls.append(
            PlannedCall(
                tool="query_records",
                input={
                    "operation": "calls_between",
                    "params": {"phone_a": hints.phones[0], "phone_b": hints.phones[1], **window},
                },
            )
        )
    elif hints.phones:
        calls.append(
            PlannedCall(
                tool="query_records",
                input={
                    "operation": "calls_for_phone",
                    "params": {"phone": hints.phones[0], **window},
                },
            )
        )
    for plate in hints.plates:
        calls.append(
            PlannedCall(
                tool="query_records",
                input={"operation": "plate_sightings", "params": {"plate": plate, **window}},
            )
        )
    for item in hints.item_ids:
        calls.append(
            PlannedCall(
                tool="query_records",
                input={"operation": "custody_chain", "params": {"item_id": item}},
            )
        )
    return calls[:MAX_RECORD_PREFETCH]


def prefetch_calls(
    question: str,
    entity_limit: int = 2,
    tz: tzinfo | None = None,
    year: int | None = None,
) -> list[PlannedCall]:
    """search_evidence + search_video on the question, the record operations for the phones /
    plates / item tags it names (when `tz` and `year` are given), then get_entity per salient
    term. A question answered by records skips search_video and the lookups of the numbers and
    plates the record calls already cover."""
    query = question.strip()[:500]
    records = record_calls(question, tz, year) if tz is not None and year is not None else []
    covered = {
        str(v).casefold()
        for c in records
        for k, v in c.input["params"].items()
        if k in ("phone_a", "phone_b", "phone", "plate", "item_id")
    }
    calls = [PlannedCall(tool="search_evidence", input={"query": query})]
    if not records:
        calls.append(PlannedCall(tool="search_video", input={"query": query}))
    calls += records
    for term in salient_terms(question, entity_limit + len(covered)):
        if len([c for c in calls if c.tool == "get_entity"]) >= entity_limit:
            break
        digits = "".join(ch for ch in term if ch.isdigit())
        normalized = ("+1" + digits[-10:]) if len(digits) >= 10 else term
        if term.casefold() in covered or normalized.casefold() in covered:
            continue
        calls.append(PlannedCall(tool="get_entity", input={"name": term[:200]}))
    return calls
