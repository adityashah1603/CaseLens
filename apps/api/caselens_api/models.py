"""Pydantic models for the HTTP boundary, mirroring `docs/contracts/openapi.yaml` components.

Locators, answers and attributes are already validated against their own contracts where they
are produced (workers, agent validator), so they pass through as JSON objects here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["lead", "analyst", "viewer"]
CaseStatus = Literal["open", "closed", "archived"]
EvidenceType = Literal["pdf", "audio", "video", "csv"]
IngestJobStatus = Literal["queued", "running", "succeeded", "failed", "dead_lettered"]
EntityType = Literal["person", "vehicle", "phone", "location", "weapon"]
ConflictStatus = Literal["open", "reviewed"]
AgentRunStatus = Literal["running", "succeeded", "failed"]
AnswerStatus = Literal["answered", "insufficient_evidence"]
ToolName = Literal[
    "search_evidence",
    "search_video",
    "query_records",
    "get_entity",
    "get_timeline",
    "flag_conflict",
]
JsonObject = dict[str, Any]

ROLE_RANK: dict[str, int] = {"viewer": 1, "analyst": 2, "lead": 3}


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(_Out):
    status: Literal["ok"]
    version: str


# ---- users, cases, members ------------------------------------------------------------------


class User(_Out):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    is_demo: bool
    is_admin: bool
    created_at: datetime


class Membership(_Out):
    case_id: uuid.UUID
    case_name: str
    role: Role


class Me(_Out):
    user: User
    memberships: list[Membership]


class IngestSummary(_Out):
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0


class Case(_Out):
    id: uuid.UUID
    name: str
    description: str | None
    status: CaseStatus
    is_demo: bool
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    my_role: Role
    evidence_count: int
    last_activity_at: datetime | None
    ingest_summary: IngestSummary
    suggested_questions: list[str]


class CaseCreate(_In):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CaseUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: CaseStatus | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> CaseUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for key in ("name", "status"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} must not be null")
        return self


class CaseMember(_Out):
    case_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    email: str | None
    display_name: str | None
    added_by: uuid.UUID | None
    created_at: datetime


class CaseMemberCreate(_In):
    user_id: uuid.UUID | None = None
    # A light shape check; the address only has to match an existing user's email.
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+$", max_length=320)
    role: Role

    @model_validator(mode="after")
    def _exactly_one(self) -> CaseMemberCreate:
        if (self.user_id is None) == (self.email is None):
            raise ValueError("identify the user by exactly one of user_id or email")
        return self


class CaseMemberUpdate(_In):
    role: Role


# ---- evidence and ingest ------------------------------------------------------------------


class CustodyEntry(_Out):
    at: datetime
    action: str
    actor_user_id: uuid.UUID | None
    sha256: str | None
    note: str | None


class IngestJob(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    evidence_id: uuid.UUID
    stage: str
    status: IngestJobStatus
    attempts: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IngestEvent(_Out):
    event: Literal["job.updated", "evidence.ready", "evidence.failed"]
    case_id: uuid.UUID
    evidence_id: uuid.UUID
    job: IngestJob | None
    emitted_at: datetime


class Evidence(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    type: EvidenceType
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploaded_by: uuid.UUID
    chain_of_custody: list[CustodyEntry]
    recorded_at: datetime | None
    ingest_jobs: list[IngestJob]
    created_at: datetime


# ---- agent runs ---------------------------------------------------------------------------


class AskRequest(_In):
    question: str = Field(min_length=1, max_length=2000)


class ToolCall(_Out):
    name: ToolName
    input: JsonObject
    output_summary: str
    sql: str | None
    latency_ms: int
    error: str | None


class AgentRun(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    user_id: uuid.UUID
    question: str
    status: AgentRunStatus
    model: str | None
    plan: list[str] | None
    tool_calls: list[ToolCall]
    answer: JsonObject | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    created_at: datetime


class AgentRunSummary(_Out):
    id: uuid.UUID
    user_id: uuid.UUID
    question: str
    status: AgentRunStatus
    answer_status: AnswerStatus | None
    model: str | None
    latency_ms: int | None
    cost_usd: float | None
    created_at: datetime


# ---- timeline, entities, graph ------------------------------------------------------------


class TimelineSource(_Out):
    chunk_id: uuid.UUID
    evidence_id: uuid.UUID
    locator: JsonObject


class EventLocation(_Out):
    entity_id: uuid.UUID
    name: str


class TimelineEvent(_Out):
    id: uuid.UUID
    t_start: datetime
    t_end: datetime | None
    location: EventLocation | None
    description: str
    confidence: float | None
    needs_review: bool
    sources: list[TimelineSource]


class Entity(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    type: EntityType
    canonical_name: str
    normalized_key: str | None
    attributes: JsonObject
    mention_count: int
    created_at: datetime


class Mention(_Out):
    id: uuid.UUID
    entity_id: uuid.UUID
    chunk_id: uuid.UUID
    evidence_id: uuid.UUID
    surface_text: str
    confidence: float | None
    needs_review: bool
    locator: JsonObject
    created_at: datetime


class EntityLink(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    from_entity_id: uuid.UUID
    to_entity_id: uuid.UUID
    method: Literal["exact", "fuzzy", "llm", "manual"]
    confidence: float | None
    status: Literal["active", "reverted"]
    created_by: uuid.UUID | None
    created_at: datetime
    reverted_at: datetime | None
    reverted_by: uuid.UUID | None


class EntityDetail(Entity):
    aliases: list[str]
    mentions: list[Mention]
    links: list[EntityLink]


class GraphNode(_Out):
    id: uuid.UUID
    type: EntityType
    label: str
    mention_count: int


class GraphEdge(_Out):
    id: str
    source: uuid.UUID
    target: uuid.UUID
    kind: Literal["same_as", "co_mention"]
    weight: float
    link_id: uuid.UUID | None


class Graph(_Out):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# ---- conflicts and audit ------------------------------------------------------------------


class ConflictClaim(_Out):
    text: str
    source_refs: list[JsonObject]


class ConflictRecord(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    agent_run_id: uuid.UUID | None
    description: str
    claim_a: ConflictClaim
    claim_b: ConflictClaim
    status: ConflictStatus
    created_at: datetime


class AuditEntry(_Out):
    id: int
    ts: datetime
    user_id: uuid.UUID | None
    case_id: uuid.UUID | None
    action: str
    payload: JsonObject
    payload_hash: str
    prev_hash: str
    row_hash: str


class AuditVerification(_Out):
    valid: bool
    checked_rows: int
    first_invalid_id: int | None


# ---- pages --------------------------------------------------------------------------------


class _Page(_Out):
    next_cursor: str | None


class CaseList(_Page):
    items: list[Case]


class CaseMemberList(_Page):
    items: list[CaseMember]


class EvidenceList(_Page):
    items: list[Evidence]


class IngestJobList(_Page):
    items: list[IngestJob]


class AgentRunList(_Page):
    items: list[AgentRunSummary]


class TimelineEventList(_Page):
    items: list[TimelineEvent]


class EntityList(_Page):
    items: list[Entity]


class ConflictList(_Page):
    items: list[ConflictRecord]


class AuditEntryList(_Page):
    items: list[AuditEntry]
