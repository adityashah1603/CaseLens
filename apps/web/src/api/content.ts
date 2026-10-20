import { toApiError, type ApiConnection } from "./client.ts";

/** Thrown when the server's SHA-256 re-verification of the stored original fails (HTTP 409). */
export class IntegrityError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "IntegrityError";
  }
}

/**
 * Fetches an evidence original: `GET /content` with the bearer token, following the 307 to the
 * short-lived signed URL. The file is held as a Blob (signed URLs are never cached or reused).
 */
export async function fetchEvidenceContent(
  api: ApiConnection,
  caseId: string,
  evidenceId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const path = `/v1/cases/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(evidenceId)}/content`;
  const init: RequestInit = { redirect: "follow", cache: "no-store" };
  if (signal !== undefined) init.signal = signal;
  const response = await api.fetchRaw(path, init);
  if (response.status === 409) {
    const body: unknown = await response.json().catch(() => null);
    const err = toApiError(409, body);
    throw new IntegrityError(
      err.problem?.detail ??
        "The stored file failed its SHA-256 integrity check, so it is not served.",
    );
  }
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw toApiError(response.status, body);
  }
  return response.blob();
}
