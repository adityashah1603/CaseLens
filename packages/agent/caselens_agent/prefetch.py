"""Retrieval-first fast path: tool calls the runtime makes before the first model call.

Most questions need the same opening moves (search the evidence and the video for the question,
look up the people / plates / phones it names), and the model used to spend one or two planning
round trips choosing them. The loop runs them up front so the first model turn already sees the
observations and can often answer at once.

`salient_terms` is a plain heuristic over the question text (no model, nothing case-specific):
phone numbers, plate-like tokens, runs of capitalized words (names, places) that are not
question words, months or weekdays, and "<color> <vehicle>" descriptions.
"""

from __future__ import annotations

import re

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


def prefetch_calls(question: str, entity_limit: int = 2) -> list[PlannedCall]:
    """search_evidence + search_video on the question, then get_entity per salient term."""
    query = question.strip()[:500]
    calls = [
        PlannedCall(tool="search_evidence", input={"query": query}),
        PlannedCall(tool="search_video", input={"query": query}),
    ]
    calls += [
        PlannedCall(tool="get_entity", input={"name": term[:200]})
        for term in salient_terms(question, entity_limit)
    ]
    return calls
