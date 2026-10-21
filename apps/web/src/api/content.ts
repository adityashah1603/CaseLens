import { toApiError, type ApiConnection } from "./client.ts";

/** Thrown when the server's SHA-256 re-verification of the stored original fails (HTTP 409). */
export class IntegrityError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "IntegrityError";
  }
}

function contentPath(caseId: string, evidenceId: string): string {
  return `/v1/cases/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(evidenceId)}/content`;
}

/** `GET /content` with the bearer token, following the 307; 409 becomes `IntegrityError`. */
async function openContent(api: ApiConnection, caseId: string, evidenceId: string, signal?: AbortSignal): Promise<Response> {
  const init: RequestInit = { redirect: "follow", cache: "no-store" };
  if (signal !== undefined) init.signal = signal;
  const response = await api.fetchRaw(contentPath(caseId, evidenceId), init);
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
  return response;
}

/**
 * Fetches an evidence original: `GET /content` with the bearer token, following the 307 to the
 * short-lived signed URL. The file is held as a Blob (signed URLs are never cached or reused).
 * Used for PDF and CSV, which are parsed in the page.
 */
export async function fetchEvidenceContent(
  api: ApiConnection,
  caseId: string,
  evidenceId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await openContent(api, caseId, evidenceId, signal);
  return response.blob();
}

/** Where an `<audio>` / `<video>` element should load the original from. */
export type MediaSource = { kind: "signed"; url: string } | { kind: "blob"; blob: Blob };

/**
 * Resolves the short-lived signed URL for an audio or video original, so the media element loads
 * it itself (HTTP Range requests: seeking works without downloading the whole file). The API's
 * integrity check and audit still run on the `/content` request. Only the response headers are
 * read; the body is cancelled. If the API answered without a redirect (the bytes came back on
 * the authenticated route, which an element cannot call), the file is held as a Blob instead.
 * Call this each time a citation opens: the URL expires within minutes.
 */
export async function fetchEvidenceMedia(
  api: ApiConnection,
  caseId: string,
  evidenceId: string,
  signal?: AbortSignal,
): Promise<MediaSource> {
  const response = await openContent(api, caseId, evidenceId, signal);
  if (response.redirected && response.url !== "") {
    await response.body?.cancel().catch(() => undefined);
    return { kind: "signed", url: response.url };
  }
  return { kind: "blob", blob: await response.blob() };
}
