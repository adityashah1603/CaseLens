"""SYNTHETIC property and evidence log: one row per story evidence item, in item-id order.

``custody_chain`` is flattened to ``<ISO time> <holder> - <action> | ...``. An item also counts as
evidence of a story event when the event cites the evidence log, the item was collected inside
the event's time span, and (when the event has a location) at the event's location.
"""

from __future__ import annotations

from pathlib import Path

from caselens_data_gen.common import FactLocation, GeneratedFile
from caselens_data_gen.story import Story
from caselens_data_gen.story.models import Event, EvidenceItem
from caselens_data_gen.structured._shared import CsvValue, iso, plan_for, write_csv

FILE_NAME = "evidence_log.csv"
TABLE = "custody_records"
HEADER = (
    "row_id",
    "item_id",
    "description",
    "collected_by",
    "collected_at",
    "collected_location",
    "custody_chain",
)


def _holder_names(story: Story) -> dict[str, str]:
    names = {f.id: f.name for f in story.facilities}
    for person in story.people:
        label = f"{person.title} {person.name}" if person.title else person.name
        names[person.id] = f"{label} ({person.badge})" if person.badge else label
    return names


def row_values(story: Story, item: EvidenceItem, row_id: int) -> list[CsvValue]:
    holders = _holder_names(story)
    locations = {loc.id: loc for loc in story.locations}
    where = item.location_detail
    if item.location_id is not None:
        loc = locations[item.location_id]
        where = f"{loc.name}, {loc.address}: {item.location_detail}"
    chain = " | ".join(
        f"{iso(step.at)} {holders[step.holder_id]} - {step.action}" for step in item.custody_chain
    )
    return [
        row_id,
        item.item_id,
        item.description,
        holders[item.collected_by],
        iso(item.collected_at),
        where,
        chain,
    ]


def item_events(story: Story, item: EvidenceItem, plan_id: str) -> list[Event]:
    return [
        ev
        for ev in story.events
        if plan_id in ev.evidence
        and ev.t_start <= item.collected_at <= ev.t_end
        and (ev.location_id is None or ev.location_id == item.location_id)
    ]


def generate(story: Story, csv_dir: Path) -> GeneratedFile:
    plan = plan_for(story, "evidence_log")
    items = sorted(story.evidence_items, key=lambda i: i.item_id)
    path = csv_dir / FILE_NAME
    write_csv(path, HEADER, [row_values(story, item, i) for i, item in enumerate(items, start=1)])
    facts: list[FactLocation] = []
    for row_id, item in enumerate(items, start=1):
        locator = {"kind": "record", "table": TABLE, "row_id": row_id}
        facts.append(FactLocation(item.item_id, dict(locator), item.description))
        for ev in item_events(story, item, plan.id):
            facts.append(FactLocation(ev.id, dict(locator), f"item {item.item_id}"))
    return GeneratedFile(
        path=path,
        evidence_type="csv",
        description=plan.title,
        facts=facts,
        recorded_at=iso(plan.recorded_at),
    )
