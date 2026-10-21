import { describe, expect, it } from "vitest";
import { devIdentities, devSignInEnabled, devTokenEnv } from "./dev.ts";

// SYNTHETIC placeholder values, not tokens.
const env = {
  VITE_DEV_TOKEN_LEAD: " SYNTHETIC-lead ",
  VITE_DEV_TOKEN_ANALYST: "",
  VITE_DEV_TOKEN_VIEWER: undefined,
};

describe("dev sign-in", () => {
  it("is offered only in dev builds", () => {
    expect(devSignInEnabled("development", true)).toBe(true);
    expect(devSignInEnabled("test", true)).toBe(true);
    expect(devSignInEnabled("production", true)).toBe(false);
    expect(devSignInEnabled("development", false)).toBe(false);
  });

  it("reads tokens only behind import.meta.env.DEV", () => {
    expect(import.meta.env.DEV).toBe(true);
    expect(Object.keys(devTokenEnv()).sort()).toEqual([
      "VITE_DEV_TOKEN_ANALYST",
      "VITE_DEV_TOKEN_LEAD",
      "VITE_DEV_TOKEN_VIEWER",
    ]);
  });

  it("trims tokens and treats blanks as missing", () => {
    expect(devIdentities(env).map((i) => [i.key, i.token])).toEqual([
      ["lead", "SYNTHETIC-lead"],
      ["analyst", undefined],
      ["viewer", undefined],
    ]);
    expect(devIdentities({}).every((i) => i.token === undefined)).toBe(true);
  });
});
