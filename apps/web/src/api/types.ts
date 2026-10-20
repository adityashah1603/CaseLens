/**
 * Convenience aliases over the GENERATED contract types (`schema.d.ts`, from
 * `docs/contracts/openapi.yaml` via `pnpm gen:api`). Nothing here is hand-written shape.
 */
import type { components } from "./schema";

type S = components["schemas"];

export type Role = S["Role"];
export type Me = S["Me"];
export type Case = S["Case"];
export type CaseList = S["CaseList"];
export type Evidence = S["Evidence"];
export type EvidenceList = S["EvidenceList"];
export type EvidenceType = S["EvidenceType"];
export type IngestJob = S["IngestJob"];
export type IngestEvent = S["IngestEvent"];
export type AgentRun = S["AgentRun"];
export type AgentRunSummary = S["AgentRunSummary"];
export type AgentRunList = S["AgentRunList"];
export type AnswerContract = S["AnswerContract"];
export type Claim = S["Claim"];
export type Citation = S["Citation"];
export type Conflict = S["Conflict"];
export type ConflictSide = S["ConflictSide"];
export type ToolCall = S["ToolCall"];
export type Locator = S["Locator"];
export type PdfLocator = S["PdfLocator"];
export type AudioLocator = S["AudioLocator"];
export type VideoLocator = S["VideoLocator"];
export type RecordLocator = S["RecordLocator"];
export type TimelineEvent = S["TimelineEvent"];
export type TimelineEventList = S["TimelineEventList"];
export type Entity = S["Entity"];
export type EntityList = S["EntityList"];
export type EntityType = S["EntityType"];
export type EntityDetail = S["EntityDetail"];
export type EntityLink = S["EntityLink"];
export type Mention = S["Mention"];
export type Graph = S["Graph"];
export type GraphNode = S["GraphNode"];
export type GraphEdge = S["GraphEdge"];
export type AuditEntry = S["AuditEntry"];
export type AuditEntryList = S["AuditEntryList"];
export type AuditVerification = S["AuditVerification"];
export type Problem = S["Problem"];
export type BBox = S["BBox"];
