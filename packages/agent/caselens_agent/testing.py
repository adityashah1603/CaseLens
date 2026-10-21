"""Test helpers: a scripted fake model and fake tool backends. SYNTHETIC data only."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from caselens_agent.router import ModelRouter, RouterSettings, Task
from caselens_agent.tools import Conn, ToolBackends

Reply = dict[str, Any] | Callable[[str], dict[str, Any]]


@dataclass
class Call:
    task: Task
    system: str
    user: str


@dataclass
class ScriptedModel:
    """Replays scripted JSON replies. A reply may be a callable of the user prompt.

    Planning turns (schema AgentAction, whatever the router task) take the `plan` script; answer
    drafts take `synthesize`. When the plan script runs out the model finishes; the synthesize
    script repeats its last reply."""

    plan: list[Reply] = field(default_factory=list)
    synthesize: list[Reply] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    turns: list[bool] = field(default_factory=list)  # per call: was it a planning turn

    def __call__(self, task: Task, system: str, user: str, schema: type[BaseModel]) -> str:
        self.calls.append(Call(task, system, user))
        planning = schema.__name__ == "AgentAction"
        self.turns.append(planning)
        n = sum(1 for t in self.turns if t == planning) - 1
        script = self.plan if planning else self.synthesize
        if planning and n >= len(script):
            return json.dumps({"thought": "done", "action": "finish"})
        reply = script[min(n, len(script) - 1)]
        return json.dumps(reply(user) if callable(reply) else reply)

    def router(self) -> ModelRouter:
        return ModelRouter(settings=RouterSettings(llm_provider="fake"), fake=self)

    def of(self, task: Task) -> list[Call]:
        return [c for c in self.calls if c.task == task]


def tool_call(tool: str, args: dict[str, Any], thought: str = "next") -> dict[str, Any]:
    return {"thought": thought, "action": "call_tool", "tool": tool, "input": args}


@dataclass
class FakeBackends:
    """Canned search / records results, recording every call."""

    evidence: list[dict[str, Any]] = field(default_factory=list)
    video: list[dict[str, Any]] = field(default_factory=list)
    records: dict[str, Any] | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def backends(self) -> ToolBackends:
        def search_evidence(conn: Conn, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
            self.calls.append(("search_evidence", dict(args)))
            return {"results": self.evidence}

        def search_video(conn: Conn, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
            self.calls.append(("search_video", dict(args)))
            return {"results": self.video}

        def query_records(conn: Conn, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
            self.calls.append(("query_records", dict(args)))
            return self.records or {
                "operation": args["operation"],
                "sql": "SELECT 1",
                "params": {},
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "sources": [],
            }

        return ToolBackends(search_evidence, search_video, query_records)


def evidence_result(
    text: str,
    page: int = 1,
    chunk_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    score: float = 0.9,
) -> dict[str, Any]:
    return {
        "chunk_id": str(chunk_id or uuid.uuid4()),
        "evidence_id": str(evidence_id or uuid.uuid4()),
        "modality": "pdf",
        "text": text,
        "score": score,
        "locator": {"kind": "pdf", "page": page, "bbox": None, "ocr_confidence": None},
    }


def plate_records(
    plate: str, rows: list[tuple[str, str, int]], evidence_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """plate_sightings output for (read_at, location_name, row_id) rows."""
    ev = str(evidence_id or uuid.uuid4())
    return {
        "operation": "plate_sightings",
        "sql": "SELECT ... WHERE case_id = %s AND plate = %s",
        "params": {"plate": plate},
        "columns": ["plate", "read_at", "location_name"],
        "rows": [{"plate": plate, "read_at": t, "location_name": loc} for t, loc, _ in rows],
        "row_count": len(rows),
        "truncated": False,
        "sources": [
            {"table": "plate_reads", "record_id": str(uuid.uuid4()), "evidence_id": ev, "row_id": r}
            for _, _, r in rows
        ],
    }
