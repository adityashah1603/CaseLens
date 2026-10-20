import type { AuthSession } from "./types.ts";

export interface DevIdentity {
  key: "lead" | "analyst" | "viewer";
  label: string;
  description: string;
  token: string | undefined;
}

type DevEnv = Pick<ImportMetaEnv, "VITE_DEV_TOKEN_LEAD" | "VITE_DEV_TOKEN_ANALYST" | "VITE_DEV_TOKEN_VIEWER">;

/** Dev sign-in is offered only outside production builds. */
export function devSignInEnabled(mode: string = import.meta.env.MODE): boolean {
  return mode !== "production";
}

/**
 * The named dev identities. Tokens come from `apps/web/.env.local` (gitignored), minted by
 * `python -m caselens_api seed-dev-users`.
 */
export function devIdentities(env: DevEnv = import.meta.env): DevIdentity[] {
  const clean = (v: string | undefined): string | undefined => (v === undefined || v.trim() === "" ? undefined : v.trim());
  return [
    {
      key: "lead",
      label: "Dev lead",
      description: "Case lead: everything, including the audit log.",
      token: clean(env.VITE_DEV_TOKEN_LEAD),
    },
    {
      key: "analyst",
      label: "Dev analyst",
      description: "Analyst: upload evidence and revert entity merges.",
      token: clean(env.VITE_DEV_TOKEN_ANALYST),
    },
    {
      key: "viewer",
      label: "Dev viewer",
      description: "Read-only viewer: may ask questions, no mutations.",
      token: clean(env.VITE_DEV_TOKEN_VIEWER),
    },
  ];
}

export function devSession(token: string, label: string): AuthSession {
  return { token: token.trim(), label, source: "dev" };
}
