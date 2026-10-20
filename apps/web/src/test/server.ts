/** MSW mock of the CaseLens API (SYNTHETIC fixtures), shared by every test. */
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { Role } from "../api/types.ts";
import {
  answerFixture,
  auditEntries,
  CASE_ID,
  caseFixture,
  ENTITIES,
  entityDetail,
  EV_TAMPERED,
  EVIDENCE,
  GRAPH,
  me,
  PLATE_CSV,
  runFixture,
  TIMELINE,
} from "./fixtures.ts";

export const API = "http://127.0.0.1:8000";
export const STORAGE = "http://storage.test";

export interface MockState {
  role: Role;
  isAdmin: boolean;
  /** Every Authorization header the mock API received, in order. */
  authHeaders: (string | null)[];
  answer: ReturnType<typeof answerFixture>;
}

export const mockState: MockState = {
  role: "lead",
  isAdmin: false,
  authHeaders: [],
  answer: answerFixture(),
};

export function resetMockState(): void {
  mockState.role = "lead";
  mockState.isAdmin = false;
  mockState.authHeaders = [];
  mockState.answer = answerFixture();
}

const problem = (status: number, title: string, detail: string) =>
  HttpResponse.json({ type: "about:blank", title, status, detail }, { status, headers: { "Content-Type": "application/problem+json" } });

function record(request: Request): void {
  mockState.authHeaders.push(request.headers.get("Authorization"));
}

const C = `${API}/v1/cases/:caseId`;

export const handlers = [
  http.get(`${API}/v1/me`, ({ request }) => {
    record(request);
    return HttpResponse.json(me(mockState.isAdmin));
  }),
  http.get(`${API}/v1/cases`, ({ request }) => {
    record(request);
    return HttpResponse.json({ items: [caseFixture(mockState.role)], next_cursor: null });
  }),
  http.get(C, ({ request, params }) => {
    record(request);
    if (params["caseId"] !== CASE_ID) return problem(404, "Not found", "No such case.");
    return HttpResponse.json(caseFixture(mockState.role));
  }),
  http.get(`${C}/runs`, ({ request }) => {
    record(request);
    return HttpResponse.json({ items: [], next_cursor: null });
  }),
  http.post(`${C}/ask`, async ({ request }) => {
    record(request);
    const body = (await request.json()) as { question: string };
    return HttpResponse.json({ ...runFixture(mockState.answer), question: body.question });
  }),
  http.get(`${C}/evidence`, ({ request }) => {
    record(request);
    return HttpResponse.json({ items: Object.values(EVIDENCE).filter((e) => e.id !== EV_TAMPERED), next_cursor: null });
  }),
  http.get(`${C}/evidence/:evidenceId`, ({ request, params }) => {
    record(request);
    const ev = EVIDENCE[String(params["evidenceId"])];
    return ev === undefined ? problem(404, "Not found", "No such evidence.") : HttpResponse.json(ev);
  }),
  http.get(`${C}/evidence/:evidenceId/content`, ({ request, params }) => {
    record(request);
    const id = String(params["evidenceId"]);
    if (id === EV_TAMPERED) return problem(409, "Integrity check failed", "Stored SHA-256 does not match the recorded hash.");
    return new HttpResponse(null, { status: 307, headers: { Location: `${STORAGE}/signed/${id}?sig=SYNTHETIC`, "Cache-Control": "no-store" } });
  }),
  http.get(`${STORAGE}/signed/:evidenceId`, ({ params }) => {
    const ev = EVIDENCE[String(params["evidenceId"])];
    if (ev === undefined) return new HttpResponse(null, { status: 404 });
    const body = ev.type === "csv" ? PLATE_CSV : `SYNTHETIC ${ev.type} bytes`;
    return new HttpResponse(body, { headers: { "Content-Type": ev.content_type } });
  }),
  http.get(`${C}/ingest/events`, ({ request }) => {
    record(request);
    return new HttpResponse(": heartbeat\n\n", { headers: { "Content-Type": "text/event-stream" } });
  }),
  http.post(`${C}/evidence`, ({ request }) => {
    record(request);
    if (mockState.role === "viewer") return problem(403, "Forbidden", "Viewers cannot upload.");
    const ev = EVIDENCE[Object.keys(EVIDENCE)[0] ?? ""];
    return HttpResponse.json(ev, { status: 201 });
  }),
  http.get(`${C}/timeline`, ({ request }) => {
    record(request);
    return HttpResponse.json({ items: TIMELINE, next_cursor: null });
  }),
  http.get(`${C}/entities`, ({ request }) => {
    record(request);
    return HttpResponse.json({ items: ENTITIES, next_cursor: null });
  }),
  http.get(`${C}/entities/:entityId`, ({ request }) => {
    record(request);
    return HttpResponse.json(entityDetail());
  }),
  http.post(`${C}/entity-links/:linkId/revert`, ({ request }) => {
    record(request);
    if (mockState.role === "viewer") return problem(403, "Forbidden", "Viewers cannot revert.");
    const link = entityDetail().links[0];
    return HttpResponse.json({ ...link, status: "reverted", reverted_at: "2026-03-21T10:00:00Z", reverted_by: null });
  }),
  http.get(`${C}/graph`, ({ request }) => {
    record(request);
    return HttpResponse.json(GRAPH);
  }),
  http.get(`${C}/audit`, ({ request }) => {
    record(request);
    if (mockState.role !== "lead") return problem(403, "Forbidden", "Lead only.");
    return HttpResponse.json({ items: auditEntries(), next_cursor: null });
  }),
];

export const server = setupServer(...handlers);
