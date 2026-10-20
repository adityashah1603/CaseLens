/**
 * SYNTHETIC test fixtures, typed by the generated contract (`schema.d.ts`). Every name, plate and
 * id here is fictional.
 */
import type {
  AgentRun,
  AnswerContract,
  AuditEntry,
  Case,
  EntityDetail,
  Entity,
  Evidence,
  Graph,
  Me,
  Role,
  TimelineEvent,
} from "../api/types.ts";

export const CASE_ID = "00000000-0000-4000-8000-0000000000c1";
export const USER_ID = "00000000-0000-4000-8000-0000000000a1";
export const EV_PDF = "00000000-0000-4000-8000-0000000000e1";
export const EV_AUDIO = "00000000-0000-4000-8000-0000000000e2";
export const EV_VIDEO = "00000000-0000-4000-8000-0000000000e3";
export const EV_CSV = "00000000-0000-4000-8000-0000000000e4";
export const EV_TAMPERED = "00000000-0000-4000-8000-0000000000e9";
export const ENT_SEDAN = "00000000-0000-4000-8000-0000000000d1";
export const ENT_PLATE = "00000000-0000-4000-8000-0000000000d2";
export const LINK_ID = "00000000-0000-4000-8000-0000000000f7";
export const RUN_ID = "00000000-0000-4000-8000-0000000000b1";
const TS = "2026-03-20T09:00:00Z";
const SHA = "a".repeat(64);

export function me(isAdmin = false): Me {
  return {
    user: { id: USER_ID, email: "lead@example.test", display_name: "SYNTHETIC Lead", is_demo: false, is_admin: isAdmin, created_at: TS },
    memberships: [{ case_id: CASE_ID, case_name: "The Riverside Lot Homicide (SYNTHETIC)", role: "lead" }],
  };
}

export function caseFixture(role: Role): Case {
  return {
    id: CASE_ID,
    name: "The Riverside Lot Homicide (SYNTHETIC)",
    description: "Fictional demo case generated from a hidden ground-truth story.",
    status: "open",
    is_demo: true,
    created_by: USER_ID,
    created_at: TS,
    updated_at: TS,
    my_role: role,
    evidence_count: 4,
    last_activity_at: TS,
    ingest_summary: { queued: 0, running: 0, succeeded: 20, failed: 0, dead_lettered: 0 },
    suggested_questions: ["Who was in the gray sedan on March 14?"],
  };
}

function evidence(id: string, type: Evidence["type"], filename: string, contentType: string): Evidence {
  return {
    id,
    case_id: CASE_ID,
    type,
    original_filename: filename,
    content_type: contentType,
    size_bytes: 2048,
    sha256: SHA,
    uploaded_by: USER_ID,
    chain_of_custody: [{ at: TS, action: "uploaded", actor_user_id: USER_ID, sha256: SHA, note: null }],
    recorded_at: null,
    ingest_jobs: [
      {
        id: `${id.slice(0, -2)}f1`,
        case_id: CASE_ID,
        evidence_id: id,
        stage: "chunk",
        status: "succeeded",
        attempts: 1,
        error: null,
        started_at: TS,
        finished_at: TS,
        created_at: TS,
        updated_at: TS,
      },
    ],
    created_at: TS,
  };
}

export const EVIDENCE: Record<string, Evidence> = {
  [EV_PDF]: evidence(EV_PDF, "pdf", "witness_statement_2.pdf", "application/pdf"),
  [EV_AUDIO]: evidence(EV_AUDIO, "audio", "interview_mercer.wav", "audio/wav"),
  [EV_VIDEO]: evidence(EV_VIDEO, "video", "camera3.mp4", "video/mp4"),
  [EV_CSV]: evidence(EV_CSV, "csv", "plate_reads.csv", "text/csv"),
  [EV_TAMPERED]: evidence(EV_TAMPERED, "pdf", "tampered.pdf", "application/pdf"),
};

export const PLATE_CSV = [
  "# SYNTHETIC DATA - fictional case. No real people, places, vehicles, phone numbers, or events.",
  "row_id,plate,plate_state,read_at,camera_id",
  "114,SYN1000,CA,2026-03-14T22:10:00-07:00,alpr_1",
  "115,SYN1001,CA,2026-03-14T22:20:00-07:00,alpr_1",
  "116,SYN1002,CA,2026-03-14T22:30:00-07:00,alpr_1",
  "117,SYN1003,CA,2026-03-14T22:35:00-07:00,alpr_1",
  "118,SYN4821,CA,2026-03-14T22:40:58-07:00,alpr_1",
  "119,SYN1004,CA,2026-03-14T22:50:00-07:00,alpr_1",
  '120,"SYN,1005",CA,2026-03-14T22:55:00-07:00,alpr_1',
  "121,SYN1006,CA,2026-03-14T23:05:00-07:00,alpr_1",
  "122,SYN1007,CA,2026-03-14T23:15:00-07:00,alpr_1",
  "",
].join("\n");

export function answerFixture(): AnswerContract {
  return {
    status: "answered",
    answer: "SYNTHETIC. The gray sedan (plate SYN4821) was driven by Dale Mercer, with one passenger.",
    claims: [
      { id: "cl1", text: "Plate SYN4821 was read entering Riverside Lot at 22:40:58.", citation_ids: ["c1"], t_start: "2026-03-14T22:40:58-07:00", t_end: null },
      { id: "cl2", text: "Camera 3 shows a gray sedan entering the lot.", citation_ids: ["c2"], t_start: null, t_end: null },
      { id: "cl3", text: "Dale Mercer says he was driving the gray sedan.", citation_ids: ["c3"], t_start: null, t_end: null },
      { id: "cl4", text: "A witness saw a driver and one passenger.", citation_ids: ["c4", "c1"], t_start: null, t_end: null },
    ],
    citations: [
      {
        id: "c1",
        source_type: "record",
        evidence_id: EV_CSV,
        chunk_id: null,
        table: "plate_reads",
        record_id: "00000000-0000-4000-8000-0000000000a9",
        locator: { kind: "record", table: "plate_reads", row_id: 118 },
        quote: null,
      },
      {
        id: "c2",
        source_type: "chunk",
        evidence_id: EV_VIDEO,
        chunk_id: "00000000-0000-4000-8000-0000000000b2",
        table: null,
        record_id: null,
        locator: { kind: "video", start_ms: 125000, end_ms: 130000, frame_idx: 3750, bbox: [412, 220, 618, 351], track_id: "t7" },
        quote: "gray sedan enters lot",
      },
      {
        id: "c3",
        source_type: "chunk",
        evidence_id: EV_AUDIO,
        chunk_id: "00000000-0000-4000-8000-0000000000b3",
        table: null,
        record_id: null,
        locator: { kind: "audio", start_ms: 41200, end_ms: 52900, speaker: "SPEAKER_01" },
        quote: "I was driving the gray car, yeah.",
      },
      {
        id: "c4",
        source_type: "chunk",
        evidence_id: EV_PDF,
        chunk_id: "00000000-0000-4000-8000-0000000000b4",
        table: null,
        record_id: null,
        locator: { kind: "pdf", page: 2, bbox: [72, 410.5, 540, 452], ocr_confidence: 0.81 },
        quote: "around 10:15 I saw a gray car pull in, two people inside",
      },
    ],
    conflicts: [
      {
        id: "cf1",
        description: "Arrival time of the gray sedan disagrees.",
        claim_a: { text: "The gray sedan arrived around 22:15.", citation_ids: ["c4"] },
        claim_b: { text: "The gray sedan arrived at 22:41.", citation_ids: ["c1", "c2"] },
        conflict_record_id: null,
      },
    ],
    confidence: 0.78,
    open_questions: ["The passenger is not identified in any cited source."],
  };
}

export function insufficientFixture(): AnswerContract {
  return {
    status: "insufficient_evidence",
    answer: "Insufficient evidence: no source in this case names the shooter's employer.",
    claims: [],
    citations: [],
    conflicts: [],
    confidence: 0.1,
    open_questions: ["No employment records are in the case file."],
  };
}

export function runFixture(answer: AnswerContract = answerFixture()): AgentRun {
  return {
    id: RUN_ID,
    case_id: CASE_ID,
    user_id: USER_ID,
    question: "Who was in the gray sedan on March 14?",
    status: "succeeded",
    model: "synthetic-model",
    plan: ["Search plate reads", "Search witness statements"],
    tool_calls: [
      {
        name: "query_records",
        input: { operation: "plate_reads_by_plate", plate: "SYN4821" },
        output_summary: "1 row",
        sql: "SELECT row_id FROM plate_reads WHERE plate = %(plate)s",
        latency_ms: 12,
        error: null,
      },
    ],
    answer,
    latency_ms: 23000,
    input_tokens: 1200,
    output_tokens: 300,
    cost_usd: 0.004,
    created_at: TS,
  };
}

export const ENTITIES: Entity[] = [
  { id: ENT_SEDAN, case_id: CASE_ID, type: "vehicle", canonical_name: "gray sedan", normalized_key: null, attributes: { color: "gray" }, mention_count: 5, created_at: TS },
  { id: ENT_PLATE, case_id: CASE_ID, type: "vehicle", canonical_name: "SYN4821", normalized_key: "SYN4821", attributes: {}, mention_count: 3, created_at: TS },
];

export function entityDetail(): EntityDetail {
  const first = ENTITIES[0];
  if (first === undefined) throw new Error("fixture");
  return {
    ...first,
    aliases: ["gray car"],
    mentions: [
      {
        id: "00000000-0000-4000-8000-0000000000a5",
        entity_id: ENT_SEDAN,
        chunk_id: "00000000-0000-4000-8000-0000000000b4",
        evidence_id: EV_PDF,
        surface_text: "gray car",
        confidence: 0.9,
        needs_review: false,
        locator: { kind: "pdf", page: 2, bbox: [72, 410.5, 540, 452], ocr_confidence: 0.81 },
        created_at: TS,
      },
    ],
    links: [
      {
        id: LINK_ID,
        case_id: CASE_ID,
        from_entity_id: ENT_SEDAN,
        to_entity_id: ENT_PLATE,
        method: "fuzzy",
        confidence: 0.8,
        status: "active",
        created_by: null,
        created_at: TS,
        reverted_at: null,
        reverted_by: null,
      },
    ],
  };
}

export const GRAPH: Graph = {
  nodes: [
    { id: ENT_SEDAN, type: "vehicle", label: "gray sedan", mention_count: 5 },
    { id: ENT_PLATE, type: "vehicle", label: "SYN4821", mention_count: 3 },
  ],
  edges: [{ id: LINK_ID, source: ENT_SEDAN, target: ENT_PLATE, kind: "same_as", weight: 1, link_id: LINK_ID }],
};

export const TIMELINE: TimelineEvent[] = [
  {
    id: "00000000-0000-4000-8000-0000000000aa",
    t_start: "2026-03-14T22:40:58-07:00",
    t_end: null,
    location: { entity_id: "00000000-0000-4000-8000-0000000000ab", name: "Riverside Lot" },
    description: "Plate SYN4821 read entering Riverside Lot.",
    confidence: 0.95,
    needs_review: false,
    sources: [{ chunk_id: "00000000-0000-4000-8000-0000000000ac", evidence_id: EV_CSV, locator: { kind: "record", table: "plate_reads", row_id: 118 } }],
  },
];

export function auditEntries(): AuditEntry[] {
  const h = (c: string) => c.repeat(64);
  return [
    { id: 7, ts: TS, user_id: USER_ID, case_id: CASE_ID, action: "agent.ask", payload: { question_sha256: h("1") }, payload_hash: h("2"), prev_hash: h("0"), row_hash: h("3") },
    { id: 8, ts: TS, user_id: null, case_id: CASE_ID, action: "evidence.view", payload: { evidence_id: EV_PDF }, payload_hash: h("4"), prev_hash: h("3"), row_hash: h("5") },
  ];
}
