"""Strict JSON schemas for LLM output (extraction and adjudication). Validated by the router."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EntityKind = Literal["person", "vehicle", "phone", "location", "weapon"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityAttributes(_Strict):
    """Explicit fields (not a free-form map) so every provider's JSON-schema mode accepts it."""

    role: str | None = Field(default=None, description="person: witness, suspect, officer, ...")
    color: str | None = Field(default=None, description="vehicle color")
    make: str | None = None
    model: str | None = None
    year: str | None = None
    plate: str | None = Field(default=None, description="vehicle license plate, if stated")
    description: str | None = Field(default=None, description="short physical description")


class ExtractedEntity(_Strict):
    type: EntityKind
    name: str = Field(min_length=1, description="most complete name used in the evidence")
    aliases: list[str] = Field(default_factory=list)
    attributes: EntityAttributes = Field(default_factory=EntityAttributes)
    surface_text: str = Field(min_length=1, description="exact text span as it appears")
    chunk_ref: int = Field(description="number N of the 'chunk N' block it appears in")
    confidence: float = Field(ge=0, le=1)


class ExtractedEvent(_Strict):
    description: str = Field(min_length=1)
    time_text: str | None = Field(default=None, description="time words exactly as written")
    t_start: str | None = Field(default=None, description="ISO 8601 with offset, if resolvable")
    t_end: str | None = Field(default=None, description="ISO 8601 with offset, if resolvable")
    happening_now: bool = Field(
        default=False,
        description="true if the block narrates the event live, as it is being recorded",
    )
    location: str | None = None
    participants: list[str] = Field(default_factory=list)
    chunk_refs: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class ExtractionOutput(_Strict):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)


class AdjudicationOutput(_Strict):
    same_entity: bool
    confidence: float = Field(ge=0, le=1)
