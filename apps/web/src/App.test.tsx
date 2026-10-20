import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockState } from "./test/server.ts";
import { renderApp, TEST_TOKEN } from "./test/render.tsx";

describe("App shell", () => {
  it("shows the SYNTHETIC DATA banner on the sign-in screen", () => {
    renderApp("/", null);
    expect(screen.getByRole("status", { name: "Synthetic data notice" })).toHaveTextContent("SYNTHETIC DATA");
    expect(screen.getByRole("heading", { name: "Sign in to CaseLens" })).toBeInTheDocument();
  });

  it("shows the SYNTHETIC DATA banner when signed in, on every route", async () => {
    const { unmount } = renderApp("/");
    expect(await screen.findByRole("heading", { name: "Your cases" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Synthetic data notice" })).toHaveTextContent("SYNTHETIC DATA");
    unmount();
    renderApp("/cases/00000000-0000-4000-8000-0000000000c1");
    expect(await screen.findByRole("heading", { name: /Riverside Lot Homicide/ })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Synthetic data notice" })).toBeInTheDocument();
  });

  it("offers dev identities and a paste-a-token field outside production", () => {
    renderApp("/", null);
    for (const role of ["lead", "analyst", "viewer"]) {
      expect(screen.getByRole("button", { name: `Sign in as ${role}` })).toBeInTheDocument();
    }
    expect(screen.getByLabelText("Or paste a token")).toBeInTheDocument();
  });

  it("signs in with a pasted token and sends it as the bearer token", async () => {
    const user = userEvent.setup();
    renderApp("/", null);
    await user.type(screen.getByLabelText("Or paste a token"), "pasted.synthetic.token");
    await user.click(screen.getByRole("button", { name: "Use token" }));
    expect(await screen.findByRole("heading", { name: "Your cases" })).toBeInTheDocument();
    await waitFor(() => {
      expect(mockState.authHeaders.length).toBeGreaterThan(0);
    });
    expect(new Set(mockState.authHeaders)).toEqual(new Set(["Bearer pasted.synthetic.token"]));
  });

  it("lists cases with role, evidence count and ingest status", async () => {
    renderApp("/");
    const link = await screen.findByRole("link", { name: /Riverside Lot Homicide/ });
    expect(link).toHaveTextContent("lead");
    expect(link).toHaveTextContent("4 evidence files");
    expect(link).toHaveTextContent("ingested");
    expect(mockState.authHeaders.every((h) => h === `Bearer ${TEST_TOKEN}`)).toBe(true);
  });
});
