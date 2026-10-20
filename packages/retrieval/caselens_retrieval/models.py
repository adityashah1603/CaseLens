"""Pydantic I/O models for the `search_evidence` and `search_video` tools.

They mirror the frozen tool contracts in
`packages/agent/caselens_agent/schemas/tools/search_evidence.json` and `search_video.json`:
`model_dump(mode="json")` of an output model validates against the tool's `output_schema`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
)

Modality = Literal["pdf", "audio", "video", "csv"]
RecordTable = Literal["call_records", "plate_reads", "custody_records"]
BBox = Annotated[list[float], Field(min_length=4, max_length=4)]


class _Locator(BaseModel):
    """A locator round-trips verbatim: optional keys absent in the stored JSON stay absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_serializer(mode="wrap")
    def _only_set_fields(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        return {k: v for k, v in data.items() if k in self.model_fields_set or k == "kind"}


class PdfLocator(_Locator):
    kind: Literal["pdf"] = "pdf"
    page: int = Field(ge=1)
    bbox: BBox | None = None
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)


class AudioLocator(_Locator):
    kind: Literal["audio"] = "audio"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str | None = None


class VideoLocator(_Locator):
    kind: Literal["video"] = "video"
    start_ms: int = Field(ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    frame_idx: int | None = Field(default=None, ge=0)
    bbox: BBox | None = None
    track_id: str | None = None


class RecordLocator(_Locator):
    kind: Literal["record"] = "record"
    table: RecordTable
    row_id: int = Field(ge=1)


Locator = Annotated[
    PdfLocator | AudioLocator | VideoLocator | RecordLocator, Field(discriminator="kind")
]
LOCATOR_ADAPTER: TypeAdapter[PdfLocator | AudioLocator | VideoLocator | RecordLocator] = (
    TypeAdapter(Locator)
)


def _unique(values: list[Any] | None) -> list[Any] | None:
    if values is not None and len(set(values)) != len(values):
        raise ValueError("items must be unique")
    return values


class EvidenceFilters(BaseModel):
    """Optional `search_evidence` filters; all given filters are ANDed.

    Times must carry a UTC offset (RFC 3339), so a window is never silently shifted.
    """

    model_config = ConfigDict(extra="forbid")

    modalities: list[Modality] | None = Field(default=None, min_length=1)
    evidence_ids: list[uuid.UUID] | None = Field(default=None, min_length=1)
    entity_ids: list[uuid.UUID] | None = Field(default=None, min_length=1)
    time_from: AwareDatetime | None = None
    time_to: AwareDatetime | None = None

    _check_unique = field_validator("modalities", "evidence_ids", "entity_ids")(_unique)


class SearchEvidenceInput(BaseModel):
    """`search_evidence` tool input (the case id is bound by the runtime, never an input)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    filters: EvidenceFilters | None = None
    top_k: int = Field(default=8, ge=1, le=20)


class SearchVideoInput(BaseModel):
    """`search_video` tool input (the case id is bound by the runtime, never an input)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    time_from: AwareDatetime | None = None
    time_to: AwareDatetime | None = None
    evidence_ids: list[uuid.UUID] | None = Field(default=None, min_length=1)
    top_k: int = Field(default=8, ge=1, le=20)

    _check_unique = field_validator("evidence_ids")(_unique)


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: uuid.UUID
    evidence_id: uuid.UUID
    modality: Modality
    text: str
    score: float
    locator: Locator


class SearchEvidenceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[EvidenceResult]


class Keyframe(BaseModel):
    """A keyframe reference. `image_uri` is null until keyframe images are stored."""

    model_config = ConfigDict(extra="forbid")

    frame_idx: int = Field(ge=0)
    bbox: BBox | None
    image_uri: str | None


class VideoResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: uuid.UUID
    evidence_id: uuid.UUID
    track_id: str | None
    summary: str
    start_ms: int = Field(ge=0)
    end_ms: int | None = Field(ge=0)
    wall_clock_start: AwareDatetime | None
    wall_clock_end: AwareDatetime | None
    keyframes: list[Keyframe]
    score: float
    locator: VideoLocator


class SearchVideoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[VideoResult]
