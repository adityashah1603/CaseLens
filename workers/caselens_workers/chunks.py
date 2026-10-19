"""Chunk output shared by every modality. Locators follow the OpenAPI `Locator` contract."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from caselens_workers.context import StageContext

BBox = Annotated[list[float], Field(min_length=4, max_length=4)]


class _Locator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfLocator(_Locator):
    kind: Literal["pdf"] = "pdf"
    page: int = Field(ge=1)
    bbox: BBox | None = None  # PDF points
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
    bbox: BBox | None = None  # pixels
    track_id: str | None = None


class RecordLocator(_Locator):
    kind: Literal["record"] = "record"
    table: Literal["call_records", "plate_reads", "custody_records"]
    row_id: int = Field(ge=0)


Locator = Annotated[
    PdfLocator | AudioLocator | VideoLocator | RecordLocator, Field(discriminator="kind")
]


class ChunkIn(BaseModel):
    """One retrieval unit. `text` is evidence content: data, never instructions."""

    model_config = ConfigDict(frozen=True)

    chunk_index: int = Field(ge=0)
    text: str
    locator: Locator
    t_start: datetime | None = None
    t_end: datetime | None = None


def replace_chunks(ctx: StageContext, chunks: Sequence[ChunkIn]) -> int:
    """Idempotently replace this evidence's chunks (and the mentions that pointed at them)."""
    ev = ctx.evidence
    with ctx.conn.transaction():
        ctx.conn.execute(
            "DELETE FROM public.mentions WHERE chunk_id IN "
            "(SELECT id FROM public.chunks WHERE evidence_id = %s)",
            (ev.id,),
        )
        ctx.conn.execute("DELETE FROM public.chunks WHERE evidence_id = %s", (ev.id,))
        with ctx.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO public.chunks (case_id, evidence_id, modality, chunk_index, text, "
                "locator, t_start, t_end) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        ev.case_id,
                        ev.id,
                        ev.type,  # chunk modality = evidence type
                        c.chunk_index,
                        c.text,
                        Jsonb(c.locator.model_dump(mode="json")),
                        c.t_start,
                        c.t_end,
                    )
                    for c in chunks
                ],
            )
    return len(chunks)
