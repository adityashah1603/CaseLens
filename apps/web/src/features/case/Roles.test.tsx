import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Role } from "../../api/types.ts";
import { CASE_ID } from "../../test/fixtures.ts";
import { renderApp } from "../../test/render.tsx";
import { mockState } from "../../test/server.ts";

async function openAs(role: Role, tab: string) {
  mockState.role = role;
  renderApp(`/cases/${CASE_ID}?tab=${tab}`);
  await screen.findByRole("heading", { name: /Riverside Lot Homicide/ });
}

describe("role-aware UI", () => {
  it.each<[Role, boolean]>([
    ["viewer", false],
    ["analyst", true],
    ["lead", true],
  ])("upload form for %s: %s", async (role, visible) => {
    await openAs(role, "evidence");
    await screen.findAllByTestId("evidence-row");
    expect(screen.queryByRole("form", { name: "Upload evidence" }) !== null).toBe(visible);
  });

  it.each<[Role, boolean]>([
    ["viewer", false],
    ["analyst", true],
    ["lead", true],
  ])("revert merge for %s: %s", async (role, visible) => {
    const user = userEvent.setup();
    await openAs(role, "entities");
    await user.click(await screen.findByRole("button", { name: /gray sedan/ }));
    await screen.findByTestId("entity-link");
    expect(screen.queryByRole("button", { name: "Revert merge" }) !== null).toBe(visible);
  });

  it.each<[Role, boolean]>([
    ["viewer", false],
    ["analyst", false],
    ["lead", true],
  ])("audit tab for %s: %s", async (role, visible) => {
    await openAs(role, "ask");
    expect(screen.queryByRole("tab", { name: "Audit" }) !== null).toBe(visible);
  });

  it("viewers may still ask questions", async () => {
    await openAs("viewer", "ask");
    expect(screen.getByRole("button", { name: "Ask" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Who was in the gray sedan on March 14?" })).toBeEnabled();
  });

  it("an analyst reverts a merge after confirming", async () => {
    const user = userEvent.setup();
    await openAs("analyst", "entities");
    await user.click(await screen.findByRole("button", { name: /gray sedan/ }));
    await user.click(await screen.findByRole("button", { name: "Revert merge" }));
    await user.click(screen.getByRole("button", { name: "Confirm revert" }));
    expect(await screen.findByRole("button", { name: "Revert merge" })).toBeInTheDocument();
    expect(mockState.authHeaders.length).toBeGreaterThan(0);
  });
});
