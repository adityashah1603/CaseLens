import { createContext, useContext } from "react";
import type { AuthState } from "./types.ts";

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const state = useContext(AuthContext);
  if (state === null) throw new Error("useAuth must be used inside <SessionProvider>");
  return state;
}
