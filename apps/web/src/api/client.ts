import createClient, { type Client } from "openapi-fetch";
import type { paths } from "./schema";
import type { Problem } from "./types.ts";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

/**
 * The API origin. `VITE_API_BASE_URL` wins when set. Otherwise a production build talks to its own
 * origin with relative URLs (the API serves the SPA), and a dev build uses the local API.
 */
export function apiBaseUrl(
  env: { fromEnv: string | undefined; prod: boolean } = {
    fromEnv: import.meta.env.VITE_API_BASE_URL,
    prod: import.meta.env.PROD,
  },
): string {
  const { fromEnv, prod } = env;
  const value = fromEnv?.trim() ?? "";
  if (value !== "") return value.replace(/\/+$/, "");
  return prod ? "" : DEFAULT_API_BASE_URL;
}

export type ApiClient = Client<paths>;

/** An HTTP error from the API, carrying the RFC 9457 problem body when the server sent one. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null, fallback: string) {
    super(problem?.detail ?? problem?.title ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }
}

function isProblem(value: unknown): value is Problem {
  return (
    typeof value === "object" &&
    value !== null &&
    "title" in value &&
    "status" in value &&
    typeof (value as { title: unknown }).title === "string"
  );
}

export function toApiError(status: number, body: unknown): ApiError {
  return new ApiError(status, isProblem(body) ? body : null, `Request failed (HTTP ${String(status)})`);
}

export interface ApiConnection {
  client: ApiClient;
  baseUrl: string;
  /** `fetch` against the API with the bearer token, for non-JSON routes (content, SSE). */
  fetchRaw: (path: string, init?: RequestInit) => Promise<Response>;
}

export interface ApiConnectionOptions {
  baseUrl?: string;
  /** The current bearer token (may be async, e.g. Firebase's cached, auto-refreshed ID token). */
  getToken: () => string | null | Promise<string | null>;
  /**
   * Mints a fresh token after a 401 (Firebase: `getIdToken(true)`). When set, the request is
   * retried once with the new token before `onUnauthorized` fires.
   */
  refreshToken?: () => Promise<string | null>;
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
}

function withToken(headers: Headers, token: string | null): void {
  if (token !== null) headers.set("Authorization", `Bearer ${token}`);
}

/**
 * A typed `openapi-fetch` client that sends `Authorization: Bearer <token>` on every request. A
 * 401 triggers one retry with a refreshed token (when the session can refresh); a second 401
 * reports `onUnauthorized`.
 */
export function createApiConnection(opts: ApiConnectionOptions): ApiConnection {
  const baseUrl = opts.baseUrl ?? apiBaseUrl();
  const fetchImpl = opts.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  /** Sends one request built by `build(token)`, retrying once with a refreshed token on 401. */
  const send = async (build: (token: string | null) => Promise<Response>): Promise<Response> => {
    let response = await build(await opts.getToken());
    if (response.status === 401 && opts.refreshToken !== undefined) {
      const fresh = await opts.refreshToken().catch(() => null);
      if (fresh !== null) {
        await response.body?.cancel().catch(() => undefined);
        response = await build(fresh);
      }
    }
    if (response.status === 401) opts.onUnauthorized?.();
    return response;
  };

  const client = createClient<paths>({
    baseUrl,
    fetch: (req: Request) => {
      // Keep an unread copy so a retry can resend the body (only when a retry is possible).
      const spare = opts.refreshToken === undefined ? null : req.clone();
      let first = true;
      return send((token) => {
        const request = first || spare === null ? req : spare;
        first = false;
        withToken(request.headers, token);
        return fetchImpl(request);
      });
    },
  });

  const fetchRaw = (path: string, init: RequestInit = {}): Promise<Response> =>
    send((token) => {
      const headers = new Headers(init.headers);
      withToken(headers, token);
      return fetchImpl(`${baseUrl}${path}`, { ...init, headers });
    });

  return { client, baseUrl, fetchRaw };
}

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Resolves an `openapi-fetch` result to its data, or throws an `ApiError`. */
export async function unwrap<T>(promise: Promise<FetchResult<T>>): Promise<T> {
  const { data, error, response } = await promise;
  if (!response.ok || error !== undefined) throw toApiError(response.status, error);
  return data as T;
}
