"""Pydantic models for the `query_records` tool output and the compiler's error type."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OperationName = Literal[
    "calls_between",
    "calls_for_phone",
    "call_count_between",
    "plate_sightings",
    "plate_sightings_near",
    "custody_chain",
]
CitableTable = Literal["call_records", "plate_reads", "custody_records"]
BoundValue = str | int | float | bool | None


class OperationError(ValueError):
    """An unknown operation or invalid params. The message is safe to show to the LLM."""


class RecordSource(BaseModel):
    """Citation for one returned record (a `record` locator: table + row_id)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: CitableTable
    record_id: uuid.UUID
    evidence_id: uuid.UUID
    row_id: int = Field(ge=1)


class QueryRecordsOutput(BaseModel):
    """Result of one semantic-layer operation; matches the `query_records` output_schema."""

    model_config = ConfigDict(extra="forbid")

    operation: OperationName
    sql: str
    params: dict[str, BoundValue]
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(ge=0)
    truncated: bool
    sources: list[RecordSource]
