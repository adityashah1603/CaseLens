"""Pydantic models for a SYNTHETIC ground-truth case story.

A story is the hidden truth that every evidence generator reads from. It never goes into the app;
it only feeds generation and the eval gold set. Every id is a stable string so generators and the
answer key can point at the same facts.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PHONE_PATTERN = re.compile(r"^\+1\d{3}55501\d{2}$")
"""Fictional NANP range only: +1-XXX-555-0100 through +1-XXX-555-0199, stored as E.164."""

PLATE_PREFIX = "SYN"

Role = Literal["victim", "suspect", "witness", "red_herring", "associate", "officer", "dispatcher"]
EvidenceType = Literal["pdf", "audio", "video", "csv"]
EvidenceKind = Literal[
    "incident_report",
    "supplemental_report",
    "witness_statement",
    "witness_statement_scanned",
    "911_call",
    "suspect_interview",
    "cctv_clip",
    "call_detail_records",
    "plate_reader_hits",
    "evidence_log",
]
CameraKind = Literal["cctv", "dashcam", "alpr"]
CallType = Literal["voice", "sms"]

KIND_TYPES: dict[str, EvidenceType] = {
    "incident_report": "pdf",
    "supplemental_report": "pdf",
    "witness_statement": "pdf",
    "witness_statement_scanned": "pdf",
    "911_call": "audio",
    "suspect_interview": "audio",
    "cctv_clip": "video",
    "call_detail_records": "csv",
    "plate_reader_hits": "csv",
    "evidence_log": "csv",
}

NON_CAST_ROLES: frozenset[str] = frozenset({"officer", "dispatcher"})


class StoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Person(StoryModel):
    id: str
    name: str
    role: Role
    age: int | None = None
    title: str | None = Field(default=None, description="Rank or job title, e.g. 'Detective'.")
    badge: str | None = None
    occupation: str | None = None
    description: str = Field(description="Appearance, relationships, and what they did.")
    aliases: list[str] = Field(default_factory=list, description="Names used in testimony.")


class Vehicle(StoryModel):
    id: str
    make: str
    model: str
    year: int
    color: str
    plate: str
    plate_state: str
    owner_id: str
    description: str
    aliases: list[str] = Field(default_factory=list, description="How witnesses refer to it.")


class Phone(StoryModel):
    id: str
    number: str = Field(description="E.164, fictional 555-01xx range only.")
    owner_id: str | None = Field(default=None, description="None when the subscriber is unknown.")
    carrier: str
    description: str


class Location(StoryModel):
    id: str
    name: str
    kind: str
    address: str = Field(description="Fictional address in the fictional city.")
    description: str
    latitude: float
    longitude: float
    aliases: list[str] = Field(default_factory=list)


class Facility(StoryModel):
    """A custody holder that is not a person (property room, crime lab, impound)."""

    id: str
    name: str


class ClipWindow(StoryModel):
    evidence_id: str
    start: AwareDatetime
    end: AwareDatetime


class Camera(StoryModel):
    id: str
    kind: CameraKind
    location_id: str
    name: str = Field(description="Human-readable camera location, used in CSV exports.")
    description: str
    latitude: float
    longitude: float
    clips: list[ClipWindow] = Field(default_factory=list)


class Event(StoryModel):
    id: str
    t_start: AwareDatetime
    t_end: AwareDatetime
    location_id: str | None = Field(default=None, description="None for phone-only events.")
    participants: list[str]
    vehicle_ids: list[str] = Field(default_factory=list)
    description: str
    evidence: list[str] = Field(min_length=1, description="evidence_plan ids that mention it.")


class Claim(StoryModel):
    evidence_id: str
    text: str


class Conflict(StoryModel):
    id: str
    description: str
    claim_a: Claim
    claim_b: Claim
    truth: str


class StoryCall(StoryModel):
    """A story call to embed in the call detail records among noise rows."""

    id: str
    caller_phone_id: str
    callee_phone_id: str
    started_at: AwareDatetime
    duration_s: int = Field(ge=0)
    call_type: CallType = "voice"
    cell_tower_id: str | None = None
    event_id: str | None = None
    note: str


class PlateRead(StoryModel):
    """A story ALPR hit to embed in the plate reader export among noise rows."""

    id: str
    vehicle_id: str | None = Field(default=None, description="True vehicle, even for misreads.")
    plate: str = Field(description="Plate text as read by the camera (may be a misread).")
    plate_state: str
    read_at: AwareDatetime
    camera_id: str
    direction: str
    confidence: float = Field(ge=0, le=1)
    event_id: str | None = None
    note: str


class CustodyStep(StoryModel):
    at: AwareDatetime
    holder_id: str = Field(description="Person id or facility id.")
    action: str


class EvidenceItem(StoryModel):
    """One row of the physical evidence log (chain of custody)."""

    item_id: str = Field(pattern=r"^ITEM-\d{3}$")
    description: str
    collected_by: str
    collected_at: AwareDatetime
    location_id: str | None = None
    location_detail: str
    custody_chain: list[CustodyStep] = Field(min_length=1)
    linked_ids: list[str] = Field(
        default_factory=list, description="Entity or evidence_plan ids this item relates to."
    )


class KeyLine(StoryModel):
    """A short quote that must appear (verbatim or near-verbatim) in the generated evidence."""

    speaker_id: str | None = None
    text: str
    approx_offset_s: int | None = Field(default=None, ge=0, description="Audio offset hint.")


class TrackFact(StoryModel):
    """A track-level fact a video clip must show (and its track summary must state)."""

    track_id: str
    label: str
    entity_id: str | None = None
    t_start: AwareDatetime
    t_end: AwareDatetime
    description: str


class EvidencePlanItem(StoryModel):
    id: str
    type: EvidenceType
    kind: EvidenceKind
    title: str
    author_id: str | None = None
    subject_ids: list[str] = Field(
        default_factory=list, description="Statement giver, caller, or interviewee."
    )
    speaker_ids: list[str] = Field(default_factory=list)
    camera_id: str | None = None
    recorded_at: AwareDatetime
    duration_s: int | None = None
    covers_events: list[str] = Field(default_factory=list)
    covers_entities: list[str] = Field(default_factory=list)
    content_notes: list[str] = Field(default_factory=list)
    key_lines: list[KeyLine] = Field(default_factory=list)
    tracks: list[TrackFact] = Field(default_factory=list)
    inconsistent: bool = Field(default=False, description="Deliberately wrong statement.")
    rendering: str | None = Field(default=None, description="Noise/scan instructions.")


class Unanswerable(StoryModel):
    id: str
    question: str
    reason: str


class Story(StoryModel):
    notice: str
    case_slug: str
    case_name: str
    case_number: str
    agency: str
    city: str
    county: str
    timezone: str
    primary_date: str
    summary: str = Field(description="Hidden truth. Never ingested into the app.")
    people: list[Person]
    vehicles: list[Vehicle]
    phones: list[Phone]
    locations: list[Location]
    facilities: list[Facility] = Field(default_factory=list)
    cameras: list[Camera]
    events: list[Event]
    conflicts: list[Conflict]
    calls: list[StoryCall]
    plate_reads: list[PlateRead]
    evidence_items: list[EvidenceItem]
    evidence_plan: list[EvidencePlanItem]
    unanswerable: list[Unanswerable]
    demo_questions: list[str]

    def cast(self) -> list[Person]:
        """People who count toward the cast (officers and dispatchers excluded)."""
        return [p for p in self.people if p.role not in NON_CAST_ROLES]

    def reference_errors(self) -> list[str]:
        """Return every unresolved id or inconsistency; empty means the story is coherent."""
        errors: list[str] = []
        person = {p.id for p in self.people}
        vehicle = {v.id for v in self.vehicles}
        phone = {p.id for p in self.phones}
        location = {loc.id for loc in self.locations}
        facility = {f.id for f in self.facilities}
        camera = {c.id: c for c in self.cameras}
        events = {e.id: e for e in self.events}
        event = set(events)
        plan = {p.id: p for p in self.evidence_plan}
        entity = person | vehicle | phone | location

        groups: list[tuple[str, list[str]]] = [
            ("people", [p.id for p in self.people]),
            ("vehicles", [v.id for v in self.vehicles]),
            ("phones", [p.id for p in self.phones]),
            ("locations", [loc.id for loc in self.locations]),
            ("facilities", [f.id for f in self.facilities]),
            ("cameras", list(camera)),
            ("events", [e.id for e in self.events]),
            ("conflicts", [c.id for c in self.conflicts]),
            ("calls", [c.id for c in self.calls]),
            ("plate_reads", [r.id for r in self.plate_reads]),
            ("evidence_items", [i.item_id for i in self.evidence_items]),
            ("evidence_plan", [p.id for p in self.evidence_plan]),
            ("unanswerable", [u.id for u in self.unanswerable]),
        ]
        all_ids: list[str] = []
        for name, ids in groups:
            if len(ids) != len(set(ids)):
                errors.append(f"duplicate ids in {name}")
            all_ids.extend(ids)
        if len(all_ids) != len(set(all_ids)):
            errors.append("an id is reused across collections")

        def need(ref: str | None, valid: set[str], where: str) -> None:
            if ref is not None and ref not in valid:
                errors.append(f"{where}: unknown id {ref!r}")

        for v in self.vehicles:
            need(v.owner_id, person, f"vehicle {v.id}.owner_id")
            if not v.plate.startswith(PLATE_PREFIX):
                errors.append(f"vehicle {v.id}: plate {v.plate!r} must start with SYN")
        for ph in self.phones:
            need(ph.owner_id, person, f"phone {ph.id}.owner_id")
            if not PHONE_PATTERN.match(ph.number):
                errors.append(f"phone {ph.id}: {ph.number!r} not in the 555-01xx range")
        for cam in camera.values():
            need(cam.location_id, location, f"camera {cam.id}.location_id")
            for clip in cam.clips:
                need(clip.evidence_id, set(plan), f"camera {cam.id}.clips")
                if clip.end <= clip.start:
                    errors.append(f"camera {cam.id}: clip {clip.evidence_id} ends before start")
        for ev in self.events:
            need(ev.location_id, location, f"event {ev.id}.location_id")
            for pid in ev.participants:
                need(pid, person, f"event {ev.id}.participants")
            for vid in ev.vehicle_ids:
                need(vid, vehicle, f"event {ev.id}.vehicle_ids")
            for eid in ev.evidence:
                need(eid, set(plan), f"event {ev.id}.evidence")
                if eid in plan and ev.id not in plan[eid].covers_events:
                    errors.append(f"event {ev.id}: {eid}.covers_events does not list it")
            if ev.t_end < ev.t_start:
                errors.append(f"event {ev.id}: t_end before t_start")
        for cf in self.conflicts:
            need(cf.claim_a.evidence_id, set(plan), f"conflict {cf.id}.claim_a")
            need(cf.claim_b.evidence_id, set(plan), f"conflict {cf.id}.claim_b")
        for call in self.calls:
            need(call.caller_phone_id, phone, f"call {call.id}.caller_phone_id")
            need(call.callee_phone_id, phone, f"call {call.id}.callee_phone_id")
            need(call.event_id, event, f"call {call.id}.event_id")
        for read in self.plate_reads:
            need(read.vehicle_id, vehicle, f"plate_read {read.id}.vehicle_id")
            need(read.camera_id, set(camera), f"plate_read {read.id}.camera_id")
            need(read.event_id, event, f"plate_read {read.id}.event_id")
            if read.camera_id in camera and camera[read.camera_id].kind != "alpr":
                errors.append(f"plate_read {read.id}: camera is not an ALPR camera")
            if not read.plate.startswith(PLATE_PREFIX):
                errors.append(f"plate_read {read.id}: plate must start with SYN")
        holders = person | facility
        for item in self.evidence_items:
            need(item.collected_by, person, f"{item.item_id}.collected_by")
            need(item.location_id, location, f"{item.item_id}.location_id")
            for step in item.custody_chain:
                need(step.holder_id, holders, f"{item.item_id}.custody_chain")
            first = item.custody_chain[0]
            if first.holder_id != item.collected_by or first.at != item.collected_at:
                errors.append(f"{item.item_id}: custody chain must start with the collector")
            times = [s.at for s in item.custody_chain]
            if times != sorted(times):
                errors.append(f"{item.item_id}: custody chain out of order")
            for ref in item.linked_ids:
                need(ref, entity | set(plan), f"{item.item_id}.linked_ids")
        for p in self.evidence_plan:
            if KIND_TYPES[p.kind] != p.type:
                errors.append(f"plan {p.id}: kind {p.kind} must have type {KIND_TYPES[p.kind]}")
            need(p.author_id, person, f"plan {p.id}.author_id")
            need(p.camera_id, set(camera), f"plan {p.id}.camera_id")
            for pid in p.subject_ids + p.speaker_ids:
                need(pid, person, f"plan {p.id}.subject/speaker")
            for eid in p.covers_events:
                need(eid, event, f"plan {p.id}.covers_events")
                if eid in events and p.id not in events[eid].evidence:
                    errors.append(f"plan {p.id}: event {eid}.evidence does not list it")
            for ref in p.covers_entities:
                need(ref, entity, f"plan {p.id}.covers_entities")
            for line in p.key_lines:
                need(line.speaker_id, person, f"plan {p.id}.key_lines")
            for track in p.tracks:
                need(track.entity_id, person | vehicle, f"plan {p.id}.tracks")
            if p.kind == "cctv_clip":
                if p.camera_id is None or not p.tracks:
                    errors.append(f"plan {p.id}: video needs camera_id and tracks")
                elif p.camera_id in camera:
                    clips = [c for c in camera[p.camera_id].clips if c.evidence_id == p.id]
                    if len(clips) != 1:
                        errors.append(f"plan {p.id}: camera must list exactly one clip for it")
                    else:
                        clip = clips[0]
                        for track in p.tracks:
                            if track.t_start < clip.start or track.t_end > clip.end:
                                errors.append(f"plan {p.id}: track {track.track_id} outside clip")
        return errors

    @model_validator(mode="after")
    def _check_references(self) -> Story:
        errors = self.reference_errors()
        if errors:
            raise ValueError("story references are inconsistent: " + "; ".join(errors))
        return self
