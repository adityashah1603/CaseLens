import { describe, expect, it, vi } from "vitest";
import { mockState } from "../test/server.ts";
import { ApiError, apiBaseUrl, createApiConnection, DEFAULT_API_BASE_URL, unwrap } from "./client.ts";
import { fetchEvidenceContent, IntegrityError } from "./content.ts";
import { CASE_ID, EV_AUDIO, EV_CSV, EV_TAMPERED } from "../test/fixtures.ts";
import { fetchEvidenceMedia } from "./content.ts";
import { STORAGE } from "../test/server.ts";

describe("API client", () => {
  it("sends the bearer token on typed requests", async () => {
    const api = createApiConnection({ getToken: () => "tok-1" });
    const me = await unwrap(api.client.GET("/v1/me"));
    expect(me.user.display_name).toBe("SYNTHETIC Lead");
    expect(mockState.authHeaders).toEqual(["Bearer tok-1"]);
  });

  it("sends the bearer token on raw requests and reads the current token each time", async () => {
    let token = "tok-a";
    const api = createApiConnection({ getToken: () => token });
    await api.fetchRaw("/v1/me");
    token = "tok-b";
    await api.fetchRaw("/v1/me");
    expect(mockState.authHeaders).toEqual(["Bearer tok-a", "Bearer tok-b"]);
  });

  it("turns problem+json errors into ApiError and reports 401s", async () => {
    const onUnauthorized = vi.fn();
    const api = createApiConnection({
      getToken: () => "t",
      onUnauthorized,
      fetchImpl: () =>
        Promise.resolve(
          new Response(JSON.stringify({ title: "Unauthorized", status: 401, detail: "Token expired." }), {
            status: 401,
            headers: { "Content-Type": "application/problem+json" },
          }),
        ),
    });
    const err = await unwrap(api.client.GET("/v1/me")).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).message).toBe("Token expired.");
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("follows the content 307 to the signed URL and returns the file", async () => {
    const api = createApiConnection({ getToken: () => "tok" });
    const blob = await fetchEvidenceContent(api, CASE_ID, EV_CSV);
    expect(await blob.text()).toContain("row_id,plate");
    expect(mockState.authHeaders).toEqual(["Bearer tok"]);
  });

  it("raises IntegrityError on a 409 from the content route", async () => {
    const api = createApiConnection({ getToken: () => "tok" });
    await expect(fetchEvidenceContent(api, CASE_ID, EV_TAMPERED)).rejects.toBeInstanceOf(IntegrityError);
  });

  it("resolves media to the signed URL without keeping the body", async () => {
    const api = createApiConnection({ getToken: () => "tok" });
    expect(await fetchEvidenceMedia(api, CASE_ID, EV_AUDIO)).toEqual({
      kind: "signed",
      url: `${STORAGE}/signed/${EV_AUDIO}?sig=SYNTHETIC`,
    });
    await expect(fetchEvidenceMedia(api, CASE_ID, EV_TAMPERED)).rejects.toBeInstanceOf(IntegrityError);
  });

  it("falls back to a Blob when the content route answers without a redirect", async () => {
    const api = createApiConnection({
      getToken: () => "tok",
      fetchImpl: () => Promise.resolve(new Response("SYNTHETIC audio bytes", { status: 200 })),
    });
    const media = await fetchEvidenceMedia(api, CASE_ID, EV_AUDIO);
    expect(media.kind).toBe("blob");
  });
});

/** A fake API: 401 unless the bearer token is `good`, echoing the request body. */
function tokenGatedFetch(calls: { url: string; auth: string | null; body: string }[]): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), "http://origin.test"), init);
    const auth = request.headers.get("Authorization");
    const body = request.method === "GET" ? "" : await request.text();
    calls.push({ url: input instanceof Request ? input.url : String(input), auth, body });
    if (auth !== "Bearer good") {
      return new Response(JSON.stringify({ title: "Unauthorized", status: 401, detail: "Token expired." }), { status: 401 });
    }
    return new Response(JSON.stringify({ title: "ok", status: 200 }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
}

describe("token refresh on 401", () => {
  it("retries a typed request once with a refreshed token, resending the body", async () => {
    const calls: { url: string; auth: string | null; body: string }[] = [];
    const onUnauthorized = vi.fn();
    const refreshToken = vi.fn(() => Promise.resolve("good"));
    const api = createApiConnection({
      baseUrl: "http://origin.test",
      getToken: () => Promise.resolve("stale"),
      refreshToken,
      onUnauthorized,
      fetchImpl: tokenGatedFetch(calls),
    });
    const { response } = await api.client.POST("/v1/cases/{case_id}/ask", {
      params: { path: { case_id: CASE_ID } },
      body: { question: "SYNTHETIC question?" },
    });
    expect(response.status).toBe(200);
    expect(calls.map((c) => c.auth)).toEqual(["Bearer stale", "Bearer good"]);
    expect(calls[1]?.body).toContain("SYNTHETIC question?");
    expect(refreshToken).toHaveBeenCalledOnce();
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("retries a raw request once, then reports a second 401", async () => {
    const calls: { url: string; auth: string | null; body: string }[] = [];
    const onUnauthorized = vi.fn();
    const api = createApiConnection({
      baseUrl: "http://origin.test",
      getToken: () => "stale",
      refreshToken: () => Promise.resolve("still-bad"),
      onUnauthorized,
      fetchImpl: tokenGatedFetch(calls),
    });
    const response = await api.fetchRaw("/v1/me");
    expect(response.status).toBe(401);
    expect(calls.map((c) => c.auth)).toEqual(["Bearer stale", "Bearer still-bad"]);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("does not retry when the session cannot refresh (dev tokens)", async () => {
    const calls: { url: string; auth: string | null; body: string }[] = [];
    const onUnauthorized = vi.fn();
    const api = createApiConnection({ baseUrl: "http://origin.test", getToken: () => "stale", onUnauthorized, fetchImpl: tokenGatedFetch(calls) });
    await api.fetchRaw("/v1/me");
    expect(calls).toHaveLength(1);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});

describe("API base URL", () => {
  it("uses VITE_API_BASE_URL when set, without trailing slashes", () => {
    expect(apiBaseUrl({ fromEnv: "https://api.example.test/", prod: true })).toBe("https://api.example.test");
    expect(apiBaseUrl({ fromEnv: "https://api.example.test", prod: false })).toBe("https://api.example.test");
  });

  it("is same-origin (relative URLs) in a production build without VITE_API_BASE_URL", () => {
    expect(apiBaseUrl({ fromEnv: undefined, prod: true })).toBe("");
    expect(apiBaseUrl({ fromEnv: "  ", prod: true })).toBe("");
  });

  it("defaults to the local API in dev builds", () => {
    expect(apiBaseUrl({ fromEnv: undefined, prod: false })).toBe(DEFAULT_API_BASE_URL);
    expect(apiBaseUrl({ fromEnv: "", prod: false })).toBe(DEFAULT_API_BASE_URL);
  });

  it("sends relative URLs when the base URL is empty", async () => {
    const calls: { url: string; auth: string | null; body: string }[] = [];
    const api = createApiConnection({ baseUrl: "", getToken: () => "good", fetchImpl: tokenGatedFetch(calls) });
    expect(api.baseUrl).toBe("");
    await api.fetchRaw("/v1/cases/x/evidence/y/content");
    expect(calls[0]?.url).toBe("/v1/cases/x/evidence/y/content");
  });
});
