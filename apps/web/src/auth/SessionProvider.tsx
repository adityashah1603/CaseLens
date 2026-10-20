import { useCallback, useMemo, useState, type ReactNode } from "react";
import { AuthContext } from "./AuthContext.ts";
import type { AuthSession, AuthState } from "./types.ts";

const STORAGE_KEY = "caselens.session";

function load(): AuthSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<AuthSession>;
    if (typeof parsed.token === "string" && typeof parsed.label === "string") {
      return { token: parsed.token, label: parsed.label, source: parsed.source === "firebase" ? "firebase" : "dev" };
    }
  } catch {
    // Storage unavailable (private window, blocked site data): fall through to signed out.
  }
  return null;
}

function save(session: AuthSession | null): void {
  try {
    if (session === null) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Not persisting is fine: the session simply ends with the tab.
  }
}

/** Holds the signed-in session (tab-scoped `sessionStorage`, never `localStorage`). */
export function SessionProvider({ children, initial }: { children: ReactNode; initial?: AuthSession | null }) {
  const [session, setSession] = useState<AuthSession | null>(() => (initial === undefined ? load() : initial));
  const [notice, setNotice] = useState<string | null>(null);

  const signIn = useCallback((next: AuthSession) => {
    save(next);
    setNotice(null);
    setSession(next);
  }, []);

  const signOut = useCallback((reason?: string) => {
    save(null);
    setNotice(reason ?? null);
    setSession(null);
  }, []);

  const value = useMemo<AuthState>(() => ({ session, signIn, signOut, notice }), [session, signIn, signOut, notice]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
