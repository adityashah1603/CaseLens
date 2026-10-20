"""CSV pipeline: schema_infer -> validate -> load -> register -> embed.

* schema_infer: match the header to one typed table (call_records, plate_reads, custody_records).
* validate: one Pydantic row model per table; timestamps must carry a UTC offset, phones become
  E.164, plates are normalized. Any invalid row fails the stage, naming the row_ids.
* load: upsert into the typed table on (evidence_id, row_id), so a rerun never duplicates.
* register: one readable chunk per row (RecordLocator) so rows are searchable next to documents,
  plus pass-1 entity resolution (phone / vehicle entities keyed on the normalized identifier).
* embed: the shared embedding stage (our addition to the guide's CSV path).

Lines starting with `#` (the SYNTHETIC notice) are comments. Cell text is data, never instructions.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from caselens_workers import resolve
from caselens_workers.chunks import ChunkIn, RecordLocator, replace_chunks
from caselens_workers.context import StageContext, StageResult
from caselens_workers.pipeline import Pipeline
from caselens_workers.stages.embed import embed

Table = Literal["call_records", "plate_reads", "custody_records"]

# Columns per typed table (migration 0001), in file order; `row_id` is always first.
TABLE_COLUMNS: dict[Table, tuple[str, ...]] = {
    "call_records": (
        "row_id",
        "caller_number",
        "callee_number",
        "started_at",
        "duration_s",
        "call_type",
        "cell_tower_id",
    ),
    "plate_reads": (
        "row_id",
        "plate",
        "plate_state",
        "read_at",
        "camera_id",
        "location_name",
        "latitude",
        "longitude",
        "confidence",
    ),
    "custody_records": (
        "row_id",
        "item_id",
        "description",
        "collected_by",
        "collected_at",
        "collected_location",
        "custody_chain",
    ),
}
# NOT NULL columns: a header without them cannot be loaded into that table.
REQUIRED_COLUMNS: dict[Table, frozenset[str]] = {
    "call_records": frozenset(
        {"row_id", "caller_number", "callee_number", "started_at", "call_type"}
    ),
    "plate_reads": frozenset({"row_id", "plate", "read_at"}),
    "custody_records": frozenset({"row_id", "item_id"}),
}


class CsvSchemaError(ValueError):
    """The header does not match exactly one known table."""


class CsvValidationError(ValueError):
    """One or more rows failed validation (the message names them)."""


# ------------------------------------------------------------------------------------------
# Row models
# ------------------------------------------------------------------------------------------


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    row_id: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def _blank_is_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: (None if isinstance(v, str) and not v.strip() else v) for k, v in data.items()
            }
        return data


class CallRecordRow(_Row):
    caller_number: str
    callee_number: str
    started_at: AwareDatetime
    duration_s: int | None = Field(default=None, ge=0)
    call_type: Literal["voice", "sms"]
    cell_tower_id: str | None = None

    @field_validator("caller_number", "callee_number", mode="before")
    @classmethod
    def _phone(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        phone = resolve.normalize_phone(value)
        if phone is None:
            raise ValueError(f"not a NANP phone number: {value!r}")
        return phone

    @field_validator("call_type", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


class PlateReadRow(_Row):
    plate: str
    plate_state: str | None = None
    read_at: AwareDatetime
    camera_id: str | None = None
    location_name: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("plate", mode="before")
    @classmethod
    def _plate(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        plate = resolve.normalize_plate(value)
        if plate is None:
            raise ValueError(f"not a plate: {value!r}")
        return plate


class CustodyRecordRow(_Row):
    item_id: str
    description: str | None = None
    collected_by: str | None = None
    collected_at: AwareDatetime | None = None
    collected_location: str | None = None
    custody_chain: str | None = None


RowModel = CallRecordRow | PlateReadRow | CustodyRecordRow
ROW_MODELS: dict[Table, type[RowModel]] = {
    "call_records": CallRecordRow,
    "plate_reads": PlateReadRow,
    "custody_records": CustodyRecordRow,
}


class SchemaMapping(BaseModel):
    """schema_infer artifact: which table the file maps to and its header."""

    table: Table
    columns: list[str]
    header_line: int  # 1-based line number of the header in the file
    data_rows: int


# ------------------------------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------------------------------


def _read_records(text: str) -> tuple[list[str], list[tuple[int, list[str]]], int]:
    """Return (header, [(line_no, cells)], header_line), skipping `#` comment and blank lines.

    Line numbers are 1-based positions in the original file (a record's first line)."""
    kept = [
        (n, line)
        for n, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not kept:
        raise CsvSchemaError("CSV has no header row")
    numbers = [n for n, _ in kept]
    reader = csv.reader(io.StringIO("\n".join(line for _, line in kept)))
    parsed: list[tuple[int, list[str]]] = []
    consumed = 0  # physical lines of the joined buffer consumed so far
    for cells in reader:
        parsed.append((numbers[consumed], cells))
        consumed = reader.line_num
    header = [c.strip().lower() for c in parsed[0][1]]
    return header, parsed[1:], parsed[0][0]


def infer_table(header: Sequence[str]) -> Table:
    if not header or header[0] != "row_id":
        raise CsvSchemaError(f"first column must be row_id, got header {list(header)}")
    cols = set(header)
    if len(cols) != len(header):
        raise CsvSchemaError(f"duplicate columns in header {list(header)}")
    matches = [
        table
        for table, columns in TABLE_COLUMNS.items()
        if cols <= set(columns) and REQUIRED_COLUMNS[table] <= cols
    ]
    if len(matches) != 1:
        raise CsvSchemaError(
            f"header {list(header)} does not match exactly one table of "
            f"{sorted(TABLE_COLUMNS)} (matches: {matches})"
        )
    return matches[0]


def _text(ctx: StageContext) -> str:
    return ctx.original_path().read_text(encoding="utf-8-sig")


def _rows_from_artifact(ctx: StageContext) -> tuple[Table, list[RowModel]]:
    mapping = SchemaMapping.model_validate(ctx.get_json("schema_infer", "mapping.json"))
    model = ROW_MODELS[mapping.table]
    rows = [model.model_validate(r) for r in ctx.get_json("validate", "rows.json")]
    return mapping.table, rows


# ------------------------------------------------------------------------------------------
# Stages
# ------------------------------------------------------------------------------------------


def schema_infer(ctx: StageContext) -> StageResult:
    header, records, header_line = _read_records(_text(ctx))
    table = infer_table(header)
    mapping = SchemaMapping(
        table=table, columns=header, header_line=header_line, data_rows=len(records)
    )
    ctx.put_json("mapping.json", mapping.model_dump(mode="json"))
    return StageResult(summary={"table": table, "columns": header, "data_rows": len(records)})


def validate(ctx: StageContext) -> StageResult:
    mapping = SchemaMapping.model_validate(ctx.get_json("schema_infer", "mapping.json"))
    header, records, _ = _read_records(_text(ctx))
    if header != mapping.columns:
        raise CsvSchemaError("header changed since schema_infer")
    model = ROW_MODELS[mapping.table]
    valid: list[RowModel] = []
    errors: list[str] = []
    seen: set[int] = set()
    for line_no, cells in records:
        raw_id = cells[0].strip() if cells else ""
        label = f"row_id {raw_id}" if raw_id else f"line {line_no}"
        if len(cells) != len(header):
            errors.append(f"{label}: expected {len(header)} cells, got {len(cells)}")
            continue
        try:
            row = model.model_validate(dict(zip(header, cells, strict=True)))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors()
            )
            errors.append(f"{label}: {problems}")
            continue
        if row.row_id in seen:
            errors.append(f"{label}: duplicate row_id")
            continue
        seen.add(row.row_id)
        valid.append(row)
    if errors:
        shown = " | ".join(errors[:20])
        more = f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""
        raise CsvValidationError(f"{len(errors)} invalid row(s) in {mapping.table}: {shown}{more}")
    ctx.put_json("rows.json", [r.model_dump(mode="json") for r in valid])
    return StageResult(summary={"table": mapping.table, "valid_rows": len(valid)})


_UPSERT: dict[Table, str] = {
    "call_records": (
        "INSERT INTO public.call_records (case_id, evidence_id, row_id, caller_number, "
        "callee_number, started_at, duration_s, call_type, cell_tower_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (evidence_id, row_id) DO UPDATE SET caller_number = EXCLUDED.caller_number, "
        "callee_number = EXCLUDED.callee_number, started_at = EXCLUDED.started_at, "
        "duration_s = EXCLUDED.duration_s, call_type = EXCLUDED.call_type, "
        "cell_tower_id = EXCLUDED.cell_tower_id"
    ),
    "plate_reads": (
        "INSERT INTO public.plate_reads (case_id, evidence_id, row_id, plate, plate_state, "
        "read_at, camera_id, location_name, latitude, longitude, confidence) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (evidence_id, row_id) DO UPDATE SET plate = EXCLUDED.plate, "
        "plate_state = EXCLUDED.plate_state, read_at = EXCLUDED.read_at, "
        "camera_id = EXCLUDED.camera_id, location_name = EXCLUDED.location_name, "
        "latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, "
        "confidence = EXCLUDED.confidence"
    ),
    "custody_records": (
        "INSERT INTO public.custody_records (case_id, evidence_id, row_id, item_id, description, "
        "collected_by, collected_at, collected_location, custody_chain) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (evidence_id, row_id) DO UPDATE SET item_id = EXCLUDED.item_id, "
        "description = EXCLUDED.description, collected_by = EXCLUDED.collected_by, "
        "collected_at = EXCLUDED.collected_at, "
        "collected_location = EXCLUDED.collected_location, "
        "custody_chain = EXCLUDED.custody_chain"
    ),
}


def _values(table: Table, row: RowModel) -> tuple[Any, ...]:
    data = row.model_dump()
    return tuple(data[c] for c in TABLE_COLUMNS[table])


def load(ctx: StageContext) -> StageResult:
    table, rows = _rows_from_artifact(ctx)
    ev = ctx.evidence
    with ctx.conn.cursor() as cur:
        cur.executemany(_UPSERT[table], [(ev.case_id, ev.id, *_values(table, r)) for r in rows])
    return StageResult(summary={"table": table, "rows_loaded": len(rows)})


def _fmt(value: datetime | None) -> str:
    return "unknown time" if value is None else str(value)


def row_text(row: RowModel) -> str:
    """A readable sentence per row, so hybrid search can find records alongside documents."""
    if isinstance(row, CallRecordRow):
        verb = "called" if row.call_type == "voice" else "sent an SMS to"
        parts = [f"{row.caller_number} {verb} {row.callee_number} at {_fmt(row.started_at)}"]
        parts.append(row.call_type)
        if row.duration_s is not None:
            parts.append(f"{row.duration_s} s")
        if row.cell_tower_id:
            parts.append(f"tower {row.cell_tower_id}")
        return f"Call record row {row.row_id}: " + ", ".join(parts)
    if isinstance(row, PlateReadRow):
        plate = f"{row.plate} ({row.plate_state})" if row.plate_state else row.plate
        text = f"Plate read row {row.row_id}: plate {plate} read at {_fmt(row.read_at)}"
        if row.camera_id:
            text += f" by camera {row.camera_id}"
        if row.location_name:
            text += f" at {row.location_name}"
        if row.latitude is not None and row.longitude is not None:
            text += f" ({row.latitude}, {row.longitude})"
        if row.confidence is not None:
            text += f", read confidence {row.confidence}"
        return text
    text = f"Evidence log row {row.row_id}: {row.item_id}"
    if row.description:
        text += f" - {row.description}"
    if row.collected_by or row.collected_at or row.collected_location:
        text += f". Collected by {row.collected_by or 'unknown'} at {_fmt(row.collected_at)}"
        if row.collected_location:
            text += f", location: {row.collected_location}"
    if row.custody_chain:
        text += f". Custody chain: {row.custody_chain}"
    return text


def _row_time(row: RowModel) -> datetime | None:
    if isinstance(row, CallRecordRow):
        return row.started_at
    if isinstance(row, PlateReadRow):
        return row.read_at
    return row.collected_at


def _identifiers(row: RowModel) -> list[tuple[resolve.EntityType, str]]:
    if isinstance(row, CallRecordRow):
        return [("phone", row.caller_number), ("phone", row.callee_number)]
    if isinstance(row, PlateReadRow):
        return [("vehicle", row.plate)]
    return []


def register(ctx: StageContext) -> StageResult:
    table, rows = _rows_from_artifact(ctx)
    chunks = [
        ChunkIn(
            chunk_index=i,
            text=row_text(row),
            locator=RecordLocator(table=table, row_id=row.row_id),
            t_start=_row_time(row),
        )
        for i, row in enumerate(rows)
    ]
    ev = ctx.evidence
    with ctx.conn.transaction():
        replace_chunks(ctx, chunks)
        chunk_ids: dict[int, uuid.UUID] = dict(
            ctx.conn.execute(
                "SELECT chunk_index, id FROM public.chunks WHERE evidence_id = %s",
                (ev.id,),
            ).fetchall()
        )
        entities: dict[tuple[str, str], uuid.UUID] = {}
        mentions = 0
        for i, row in enumerate(rows):
            for entity_type, key in _identifiers(row):
                entity_id = entities.get((entity_type, key))
                if entity_id is None:
                    entity_id = resolve.ensure_entity(ctx.conn, ev.case_id, entity_type, key, key)
                    entities[(entity_type, key)] = entity_id
                resolve.add_mention(ctx.conn, ev.case_id, entity_id, chunk_ids[i], key, 1.0)
                mentions += 1
    return StageResult(
        summary={
            "table": table,
            "chunks": len(chunks),
            "entities": len(entities),
            "mentions": mentions,
        }
    )


PIPELINE = Pipeline(
    first="schema_infer",
    stages={
        "schema_infer": schema_infer,
        "validate": validate,
        "load": load,
        "register": register,
        "embed": embed,
    },
    next={
        "schema_infer": "validate",
        "validate": "load",
        "load": "register",
        "register": "embed",
        "embed": None,
    },
)
