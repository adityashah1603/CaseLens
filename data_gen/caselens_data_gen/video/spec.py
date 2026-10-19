"""Pydantic models for ``sources.json`` (what to cut from MEVA) and the per-clip sidecar JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SOURCES_PATH = Path(__file__).with_name("sources.json")

Bbox = tuple[int, int, int, int]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSpec(_Model):
    """One cached MEVA download: a keyframe-aligned frame range of one clip, or a whole clip."""

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    key: str = Field(description="Object key in the public MEVA S3 bucket.")
    frame_range: tuple[int, int] | None = Field(
        default=None, description="[first keyframe, end) in source frames; None = whole clip."
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fps: float = 30.0
    width: int
    height: int
    view: str = Field(description="What the MEVA camera actually shows.")

    @property
    def first_frame(self) -> int:
        return 0 if self.frame_range is None else self.frame_range[0]


class TrackSpec(_Model):
    """An observed track in the source footage, relabeled to the story where it fits."""

    track_id: str
    label: str
    color_as_story: str
    entity_id: str | None = None
    start_frame: int
    end_frame: int
    key_frame: int
    bbox: Bbox = Field(description="[x0, y0, x1, y1] in source pixels at key_frame.")
    summary: str = Field(description="Observed behaviour; {t:FRAME} becomes story wall-clock.")


class FactSpec(_Model):
    """A story fact the footage genuinely shows; becomes a FactLocation in the answer key."""

    fact_id: str
    track_id: str
    start_frame: int
    end_frame: int
    key_frame: int
    bbox: Bbox
    note: str


class ClipSpec(_Model):
    evidence_id: str
    case_slug: str
    source: str
    start_frame: int
    end_frame: int
    anchor_frame: int = Field(description="Source frame that maps exactly to anchor_wall_clock.")
    anchor_wall_clock: AwareDatetime
    anchor_reason: str
    out_fps: float = 15.0
    out_width: int = 960
    tracks: list[TrackSpec]
    facts: list[FactSpec] = Field(default_factory=list)
    ungrounded: list[str] = Field(
        default_factory=list, description="Story video facts this footage does not show."
    )

    @model_validator(mode="after")
    def _check_frames(self) -> ClipSpec:
        if not self.start_frame <= self.anchor_frame < self.end_frame:
            raise ValueError(f"{self.evidence_id}: anchor outside the clip")
        tracks = {t.track_id for t in self.tracks}
        spans = [(t.track_id, t.start_frame, t.end_frame, t.key_frame) for t in self.tracks]
        spans += [(f.fact_id, f.start_frame, f.end_frame, f.key_frame) for f in self.facts]
        for name, start, end, key in spans:
            if not self.start_frame <= start <= key <= end <= self.end_frame:
                raise ValueError(f"{self.evidence_id}: {name} frames outside the clip")
        for fact in self.facts:
            if fact.track_id not in tracks:
                raise ValueError(f"{self.evidence_id}: fact {fact.fact_id} has unknown track")
        return self


class SourcesFile(_Model):
    notice: str
    license: str
    attribution: str
    sources: list[SourceSpec]
    clips: list[ClipSpec]
    not_produced: dict[str, str] = Field(
        default_factory=dict, description="evidence_id -> why no MEVA footage stands in for it."
    )

    @model_validator(mode="after")
    def _check_refs(self) -> SourcesFile:
        names = {s.name: s for s in self.sources}
        for clip in self.clips:
            src = names.get(clip.source)
            if src is None:
                raise ValueError(f"{clip.evidence_id}: unknown source {clip.source}")
            if src.frame_range is not None:
                lo, hi = src.frame_range
                if not lo <= clip.start_frame < clip.end_frame <= hi:
                    raise ValueError(f"{clip.evidence_id}: clip outside the downloaded range")
        return self

    def source(self, name: str) -> SourceSpec:
        return next(s for s in self.sources if s.name == name)


def load_sources(path: Path = SOURCES_PATH) -> SourcesFile:
    return SourcesFile.model_validate(json.loads(path.read_text(encoding="utf-8")))


class SidecarKeyframe(_Model):
    frame_idx: int = Field(ge=0)
    bbox: tuple[float, float, float, float]


class SidecarTrack(_Model):
    track_id: str
    label: str
    color_as_story: str
    entity_id: str | None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    wall_clock_start: AwareDatetime
    wall_clock_end: AwareDatetime
    keyframe: SidecarKeyframe
    summary: str


class SidecarSource(_Model):
    dataset: Literal["MEVA"] = "MEVA"
    key: str
    url: str
    frame_range: tuple[int, int]
    sha256: str
    license: str
    attribution: str
    view: str


class Sidecar(_Model):
    notice: str
    evidence_id: str
    title: str
    camera_id: str
    camera_name: str
    recorded_at: AwareDatetime = Field(description="Story wall-clock of output frame 0.")
    fps: float
    frame_count: int
    duration_ms: int
    width: int
    height: int
    time_mapping: str
    source: SidecarSource
    tracks: list[SidecarTrack]
    ungrounded_story_facts: list[str]
