"""Story lookups and formatting helpers shared by the per-case document writers."""

from __future__ import annotations

from datetime import datetime

from caselens_data_gen.story import Story
from caselens_data_gen.story.models import EvidenceItem, EvidencePlanItem, Person, Phone, Vehicle


class StoryView:
    """Read-only id lookups over one story."""

    def __init__(self, story: Story) -> None:
        self.story = story
        self.people = {p.id: p for p in story.people}
        self.vehicles = {v.id: v for v in story.vehicles}
        self.phones = {p.id: p for p in story.phones}
        self.locations = {loc.id: loc for loc in story.locations}
        self.plan = {p.id: p for p in story.evidence_plan}
        self.events = {e.id: e for e in story.events}
        self.items = {i.item_id: i for i in story.evidence_items}
        holders = {p.id: officer_label(p) for p in story.people}
        holders.update({f.id: f.name for f in story.facilities})
        self.holders = holders

    def person(self, pid: str) -> Person:
        return self.people[pid]

    def vehicle(self, vid: str) -> Vehicle:
        return self.vehicles[vid]

    def phone(self, phid: str) -> Phone:
        return self.phones[phid]

    def item(self, item_id: str) -> EvidenceItem:
        return self.items[item_id]

    def plan_item(self, plan_id: str) -> EvidencePlanItem:
        return self.plan[plan_id]

    def key_line(self, plan_id: str, index: int) -> str:
        return self.plan[plan_id].key_lines[index].text

    def conflicts_for(self, plan_id: str) -> list[str]:
        return [
            c.id
            for c in self.story.conflicts
            if plan_id in (c.claim_a.evidence_id, c.claim_b.evidence_id)
        ]


def officer_label(person: Person) -> str:
    """'Detective Samuel Brandt (PCPD 0871)' for officers, plain name otherwise."""
    if person.title:
        badge = f" ({person.badge})" if person.badge else ""
        return f"{person.title} {person.name}{badge}"
    return person.name


def hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def mdy(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def stamp(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y %H:%M")


def pretty_phone(e164: str) -> str:
    """'+13605550142' -> '(360) 555-0142'."""
    digits = e164.removeprefix("+1")
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s"


def stamp_s(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y %H:%M:%S")
