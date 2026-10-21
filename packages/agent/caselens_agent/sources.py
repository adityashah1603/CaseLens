"""Sources observed during one agent run, and the short handles the model cites them by.

Every citable source a tool returns (a chunk, or a structured record row) is registered in the
run's `SourceLedger` and given a stable handle (`S1`, `S2`, ...). The model only ever sees and
writes handles; the runtime expands them back into contract citations, copying ids and locators
verbatim from the tool result. Conflicts stored by `flag_conflict` get `F1`, `F2`, ... handles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SourceType = Literal["chunk", "record"]

_SPACES = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    """Collapse whitespace and casefold, for quote checks."""
    return _SPACES.sub(" ", text).strip().casefold()


class SourceRef(BaseModel):
    """A citable source exactly as a tool returned it (contract SourceRef shape)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: SourceType
    evidence_id: str
    chunk_id: str | None = None
    table: str | None = None
    record_id: str | None = None
    locator: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        if self.source_type == "chunk":
            return ("chunk", str(self.chunk_id))
        return ("record", str(self.record_id))

    def contract_ref(self) -> dict[str, Any]:
        """The flag_conflict `source_refs[]` item for this source."""
        if self.source_type == "chunk":
            return {
                "source_type": "chunk",
                "evidence_id": self.evidence_id,
                "chunk_id": self.chunk_id,
                "locator": self.locator,
            }
        return {
            "source_type": "record",
            "evidence_id": self.evidence_id,
            "table": self.table,
            "record_id": self.record_id,
            "locator": self.locator,
        }

    @classmethod
    def chunk(cls, chunk_id: str, evidence_id: str, locator: dict[str, Any]) -> SourceRef:
        return cls(source_type="chunk", evidence_id=evidence_id, chunk_id=chunk_id, locator=locator)

    @classmethod
    def record(cls, table: str, record_id: str, evidence_id: str, row_id: int) -> SourceRef:
        return cls(
            source_type="record",
            evidence_id=evidence_id,
            table=table,
            record_id=record_id,
            locator={"kind": "record", "table": table, "row_id": row_id},
        )

    @classmethod
    def from_contract_ref(cls, ref: dict[str, Any]) -> SourceRef:
        return cls.model_validate(ref)


@dataclass
class ObservedSource:
    handle: str
    ref: SourceRef
    tool: str
    texts: list[str] = field(default_factory=list)  # text the model saw, for quote checks


@dataclass
class FlaggedConflict:
    handle: str
    conflict_id: str
    description: str
    stored: bool = True  # False: a demo-user handle, nothing was written


class SourceLedger:
    """Sources and conflicts returned by tools in one run, keyed by handle."""

    def __init__(self) -> None:
        self._by_handle: dict[str, ObservedSource] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self.flagged: dict[str, FlaggedConflict] = {}

    def add(self, ref: SourceRef, tool: str, text: str | None = None) -> str:
        """Register a source (deduplicated by chunk / record id) and return its handle."""
        handle = self._by_key.get(ref.key)
        if handle is None:
            handle = f"S{len(self._by_handle) + 1}"
            self._by_handle[handle] = ObservedSource(handle, ref, tool)
            self._by_key[ref.key] = handle
        if text:
            self._by_handle[handle].texts.append(text)
        return handle

    def add_conflict(self, conflict_id: str, description: str, stored: bool = True) -> str:
        handle = f"F{len(self.flagged) + 1}"
        self.flagged[handle] = FlaggedConflict(handle, conflict_id, description, stored)
        return handle

    def get(self, handle: str) -> ObservedSource | None:
        return self._by_handle.get(handle.strip().upper())

    def find(self, ref: SourceRef) -> ObservedSource | None:
        handle = self._by_key.get(ref.key)
        return None if handle is None else self._by_handle[handle]

    def handles(self) -> list[str]:
        return list(self._by_handle)

    def sources(self) -> list[ObservedSource]:
        return list(self._by_handle.values())

    def flagged_ids(self) -> set[str]:
        """Ids of conflict records stored in this run (unstored demo handles excluded)."""
        return {f.conflict_id for f in self.flagged.values() if f.stored}

    def __len__(self) -> int:
        return len(self._by_handle)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def observe(
    tool: str, output: dict[str, Any], ledger: SourceLedger, text_limit: int = 600
) -> tuple[dict[str, Any], list[str], int]:
    """Register the sources in a validated tool output.

    Returns the model-facing observation (ids replaced by handles, long text clipped), the
    handles it contains, and the result count.
    """
    handles: list[str] = []

    def reg(ref: SourceRef, text: str | None = None) -> str:
        h = ledger.add(ref, tool, text)
        if h not in handles:
            handles.append(h)
        return h

    if tool == "search_evidence":
        items = []
        for r in output["results"]:
            h = reg(SourceRef.chunk(r["chunk_id"], r["evidence_id"], r["locator"]), r["text"])
            items.append(
                {
                    "ref": h,
                    "modality": r["modality"],
                    "score": round(float(r["score"]), 4),
                    "locator": r["locator"],
                    "text": _clip(r["text"], text_limit),
                }
            )
        return {"results": items}, handles, len(items)

    if tool == "search_video":
        items = []
        for r in output["results"]:
            h = reg(SourceRef.chunk(r["chunk_id"], r["evidence_id"], r["locator"]), r["summary"])
            items.append(
                {
                    "ref": h,
                    "track_id": r["track_id"],
                    "summary": _clip(r["summary"], text_limit),
                    "start_ms": r["start_ms"],
                    "end_ms": r["end_ms"],
                    "wall_clock_start": r["wall_clock_start"],
                    "wall_clock_end": r["wall_clock_end"],
                    "keyframes": len(r["keyframes"]),
                    "score": round(float(r["score"]), 4),
                    "locator": r["locator"],
                }
            )
        return {"results": items}, handles, len(items)

    if tool == "query_records":
        rows: list[dict[str, Any]] = output["rows"]
        sources: list[dict[str, Any]] = output["sources"]
        aligned = len(rows) == len(sources)
        obs: dict[str, Any] = {
            "operation": output["operation"],
            "params": output["params"],
            "row_count": output["row_count"],
            "truncated": output["truncated"],
        }
        refs = []
        for i, s in enumerate(sources):
            row_text = (
                " ".join(str(v) for v in rows[i].values() if v is not None) if aligned else None
            )
            refs.append(
                reg(
                    SourceRef.record(s["table"], s["record_id"], s["evidence_id"], s["row_id"]),
                    row_text,
                )
            )
        if aligned:
            obs["rows"] = [{"ref": h, **row} for h, row in zip(refs, rows, strict=True)]
        else:
            obs["rows"] = rows
            obs["sources"] = [
                {"ref": h, "table": s["table"], "row_id": s["row_id"]}
                for h, s in zip(refs, sources, strict=True)
            ]
        return obs, handles, int(output["row_count"])

    if tool == "get_entity":
        matches = []
        for m in output["matches"]:
            mentions = [
                {
                    "ref": reg(
                        SourceRef.chunk(x["chunk_id"], x["evidence_id"], x["locator"]),
                        x["surface_text"],
                    ),
                    "surface_text": _clip(x["surface_text"], 200),
                    "confidence": x["confidence"],
                    "locator": x["locator"],
                }
                for x in m["mentions"]
            ]
            matches.append(
                {
                    "entity_id": m["entity_id"],
                    "type": m["type"],
                    "canonical_name": m["canonical_name"],
                    "attributes": m["attributes"],
                    "aliases": m["aliases"],
                    "linked_entity_ids": m["linked_entity_ids"],
                    "mentions": mentions,
                }
            )
        return {"matches": matches}, handles, len(matches)

    if tool == "get_timeline":
        events = []
        for e in output["events"]:
            refs = [
                reg(SourceRef.chunk(s["chunk_id"], s["evidence_id"], s["locator"]))
                for s in e["sources"]
            ]
            events.append(
                {
                    "event_id": e["event_id"],
                    "t_start": e["t_start"],
                    "t_end": e["t_end"],
                    "location": None if e["location"] is None else e["location"]["name"],
                    "description": _clip(e["description"], text_limit),
                    "confidence": e["confidence"],
                    "sources": refs,
                }
            )
        return {"events": events}, handles, len(events)

    if tool == "flag_conflict":
        return {"status": output["status"]}, handles, 1

    raise ValueError(f"unknown tool {tool!r}")


def fit_observation(observation: dict[str, Any], limit: int) -> str:
    """JSON-encode an observation, dropping trailing list items until it fits in `limit` chars."""
    text = json.dumps(observation, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    obs = dict(observation)
    key = next((k for k in ("results", "rows", "matches", "events") if k in obs), None)
    if key is not None:
        items = list(obs[key])
        dropped = 0
        while items and len(text) > limit:
            items.pop()
            dropped += 1
            obs[key] = items
            obs["omitted_for_length"] = dropped
            text = json.dumps(obs, ensure_ascii=False, default=str)
    return text if len(text) <= limit else _clip(text, limit)
