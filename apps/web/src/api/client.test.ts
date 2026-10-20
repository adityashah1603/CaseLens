import { describe, expect, it, vi } from "vitest";
import { mockState } from "../test/server.ts";
import { ApiError, createApiConnection, unwrap } from "./client.ts";
import { fetchEvidenceContent, IntegrityError } from "./content.ts";
import { CASE_ID, EV_CSV, EV_TAMPERED } from "../test/fixtures.ts";

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
});
