import createClient, { type Client, type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import type { Problem } from "./types.ts";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

export function apiBaseUrl(): string {
  const fromEnv = import.meta.env.VITE_API_BASE_URL;
  return fromEnv !== undefined && fromEnv !== "" ? fromEnv.replace(/\/+$/, "") : DEFAULT_API_BASE_URL;
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
  getToken: () => string | null;
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
}

/** A typed `openapi-fetch` client that sends `Authorization: Bearer <token>` on every request. */
export function createApiConnection(opts: ApiConnectionOptions): ApiConnection {
  const baseUrl = opts.baseUrl ?? apiBaseUrl();
  const fetchImpl = opts.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const client = createClient<paths>({ baseUrl, fetch: (req: Request) => fetchImpl(req) });
  const auth: Middleware = {
    onRequest({ request }) {
      const token = opts.getToken();
      if (token !== null) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    },
    onResponse({ response }) {
      if (response.status === 401) opts.onUnauthorized?.();
      return response;
    },
  };
  client.use(auth);

  const fetchRaw = async (path: string, init: RequestInit = {}): Promise<Response> => {
    const headers = new Headers(init.headers);
    const token = opts.getToken();
    if (token !== null) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetchImpl(`${baseUrl}${path}`, { ...init, headers });
    if (response.status === 401) opts.onUnauthorized?.();
    return response;
  };

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
