import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./ApiContext.ts";
import { unwrap } from "./client.ts";
import type { EntityType, EvidenceType } from "./types.ts";

export interface TimelineFilter {
  entity_id?: string;
  from?: string;
  to?: string;
}

export interface AuditFilter {
  action?: string;
  user_id?: string;
  from?: string;
  to?: string;
}

export const qk = {
  me: ["me"] as const,
  cases: ["cases"] as const,
  case: (caseId: string) => ["case", caseId] as const,
  evidenceAll: (caseId: string) => ["case", caseId, "evidence"] as const,
  evidence: (caseId: string, type: EvidenceType | null) => ["case", caseId, "evidence", type] as const,
  evidenceItem: (caseId: string, evidenceId: string) => ["case", caseId, "evidence-item", evidenceId] as const,
  runs: (caseId: string) => ["case", caseId, "runs"] as const,
  run: (caseId: string, runId: string) => ["case", caseId, "run", runId] as const,
  timeline: (caseId: string, f: TimelineFilter) => ["case", caseId, "timeline", f] as const,
  entities: (caseId: string, type: EntityType | null, q: string) => ["case", caseId, "entities", type, q] as const,
  entityAll: (caseId: string) => ["case", caseId, "entity"] as const,
  entity: (caseId: string, entityId: string) => ["case", caseId, "entity", entityId] as const,
  graph: (caseId: string) => ["case", caseId, "graph"] as const,
  audit: (caseId: string, f: AuditFilter) => ["case", caseId, "audit", f] as const,
  adminAudit: (f: AuditFilter & { case_id?: string }) => ["admin", "audit", f] as const,
};

const pageQuery = (pageParam: string | undefined) => (pageParam === undefined ? {} : { cursor: pageParam });

export function useMe() {
  const { client } = useApi();
  return useQuery({ queryKey: qk.me, queryFn: () => unwrap(client.GET("/v1/me")) });
}

export function useCases() {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.cases,
    queryFn: () => unwrap(client.GET("/v1/cases", { params: { query: { limit: 100 } } })),
  });
}

export function useCase(caseId: string) {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.case(caseId),
    queryFn: () => unwrap(client.GET("/v1/cases/{case_id}", { params: { path: { case_id: caseId } } })),
  });
}

export function useEvidenceList(caseId: string, type: EvidenceType | null) {
  const { client } = useApi();
  return useInfiniteQuery({
    queryKey: qk.evidence(caseId, type),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/v1/cases/{case_id}/evidence", {
          params: {
            path: { case_id: caseId },
            query: { limit: 100, ...(type === null ? {} : { type }), ...pageQuery(pageParam) },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useEvidenceItem(caseId: string, evidenceId: string) {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.evidenceItem(caseId, evidenceId),
    queryFn: () =>
      unwrap(
        client.GET("/v1/cases/{case_id}/evidence/{evidence_id}", {
          params: { path: { case_id: caseId, evidence_id: evidenceId } },
        }),
      ),
    staleTime: 60_000,
  });
}

export function useRuns(caseId: string) {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.runs(caseId),
    queryFn: () =>
      unwrap(client.GET("/v1/cases/{case_id}/runs", { params: { path: { case_id: caseId }, query: { limit: 20 } } })),
  });
}

export function useRun(caseId: string, runId: string | null) {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.run(caseId, runId ?? ""),
    enabled: runId !== null,
    queryFn: () =>
      unwrap(
        client.GET("/v1/cases/{case_id}/runs/{run_id}", {
          params: { path: { case_id: caseId, run_id: runId ?? "" } },
        }),
      ),
    staleTime: Infinity,
  });
}

export function useAsk(caseId: string) {
  const { client } = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ question, signal }: { question: string; signal: AbortSignal }) =>
      unwrap(
        client.POST("/v1/cases/{case_id}/ask", {
          params: { path: { case_id: caseId } },
          body: { question },
          signal,
        }),
      ),
    onSuccess: (run) => {
      queryClient.setQueryData(qk.run(caseId, run.id), run);
      void queryClient.invalidateQueries({ queryKey: qk.runs(caseId) });
    },
  });
}

export function useTimeline(caseId: string, filter: TimelineFilter) {
  const { client } = useApi();
  return useInfiniteQuery({
    queryKey: qk.timeline(caseId, filter),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/v1/cases/{case_id}/timeline", {
          params: { path: { case_id: caseId }, query: { limit: 100, ...filter, ...pageQuery(pageParam) } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useEntities(caseId: string, type: EntityType | null, q: string) {
  const { client } = useApi();
  const term = q.trim();
  return useQuery({
    queryKey: qk.entities(caseId, type, term),
    queryFn: () =>
      unwrap(
        client.GET("/v1/cases/{case_id}/entities", {
          params: {
            path: { case_id: caseId },
            query: { limit: 100, ...(type === null ? {} : { type }), ...(term === "" ? {} : { q: term }) },
          },
        }),
      ),
  });
}

export function useEntity(caseId: string, entityId: string | null) {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.entity(caseId, entityId ?? ""),
    enabled: entityId !== null,
    queryFn: () =>
      unwrap(
        client.GET("/v1/cases/{case_id}/entities/{entity_id}", {
          params: { path: { case_id: caseId, entity_id: entityId ?? "" } },
        }),
      ),
  });
}

export function useGraph(caseId: string) {
  const { client } = useApi();
  return useQuery({
    queryKey: qk.graph(caseId),
    queryFn: () => unwrap(client.GET("/v1/cases/{case_id}/graph", { params: { path: { case_id: caseId } } })),
  });
}

export function useRevertLink(caseId: string) {
  const { client } = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (linkId: string) =>
      unwrap(
        client.POST("/v1/cases/{case_id}/entity-links/{link_id}/revert", {
          params: { path: { case_id: caseId, link_id: linkId } },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.entityAll(caseId) });
      void queryClient.invalidateQueries({ queryKey: qk.graph(caseId) });
    },
  });
}

export interface UploadInput {
  file: File;
  description: string;
  /** ISO-8601 date-time, or "" when unknown. */
  recordedAt: string;
}

export function useUploadEvidence(caseId: string) {
  const { client } = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, description, recordedAt }: UploadInput) => {
      const form = new FormData();
      form.append("file", file, file.name);
      if (description.trim() !== "") form.append("description", description.trim());
      if (recordedAt !== "") form.append("recorded_at", recordedAt);
      return unwrap(
        client.POST("/v1/cases/{case_id}/evidence", {
          params: { path: { case_id: caseId } },
          body: { file: file.name },
          // Sent as multipart/form-data; the browser sets the boundary header.
          bodySerializer: () => form,
        }),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.evidenceAll(caseId) });
      void queryClient.invalidateQueries({ queryKey: qk.case(caseId) });
    },
  });
}

export function useAudit(caseId: string, filter: AuditFilter, enabled: boolean) {
  const { client } = useApi();
  return useInfiniteQuery({
    queryKey: qk.audit(caseId, filter),
    enabled,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/v1/cases/{case_id}/audit", {
          params: { path: { case_id: caseId }, query: { limit: 100, ...filter, ...pageQuery(pageParam) } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useAdminAudit(filter: AuditFilter & { case_id?: string }) {
  const { client } = useApi();
  return useInfiniteQuery({
    queryKey: qk.adminAudit(filter),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(client.GET("/v1/admin/audit", { params: { query: { limit: 100, ...filter, ...pageQuery(pageParam) } } })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useVerifyAudit() {
  const { client } = useApi();
  return useMutation({ mutationFn: () => unwrap(client.GET("/v1/admin/audit/verify")) });
}
