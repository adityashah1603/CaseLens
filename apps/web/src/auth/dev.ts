import type { AuthSession } from "./types.ts";

export interface DevIdentity {
  key: "lead" | "analyst" | "viewer";
  label: string;
  description: string;
  token: string | undefined;
}

type DevEnv = Partial<Record<"VITE_DEV_TOKEN_LEAD" | "VITE_DEV_TOKEN_ANALYST" | "VITE_DEV_TOKEN_VIEWER", string | undefined>>;

/** Dev sign-in is offered only in dev builds (`vite` / vitest), never in `vite build`. */
export function devSignInEnabled(mode: string = import.meta.env.MODE, dev: boolean = import.meta.env.DEV): boolean {
  return dev && mode !== "production";
}

/**
 * The dev tokens from `apps/web/.env.local`, read one by one behind `import.meta.env.DEV`. In a
 * production build `DEV` is the literal `false`, so the whole branch (and every token value) is
 * dropped from the bundle. Never pass `import.meta.env` as a whole object here: Vite would inline
 * every `VITE_*` value into the bundle.
 */
export function devTokenEnv(): DevEnv {
  return import.meta.env.DEV
    ? {
        VITE_DEV_TOKEN_LEAD: import.meta.env.VITE_DEV_TOKEN_LEAD,
        VITE_DEV_TOKEN_ANALYST: import.meta.env.VITE_DEV_TOKEN_ANALYST,
        VITE_DEV_TOKEN_VIEWER: import.meta.env.VITE_DEV_TOKEN_VIEWER,
      }
    : {};
}

/**
 * The named dev identities. Tokens come from `apps/web/.env.local` (gitignored), minted by
 * `python -m caselens_api seed-dev-users`, and only in dev builds (see `devTokenEnv`).
 */
export function devIdentities(env: DevEnv = devTokenEnv()): DevIdentity[] {
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
