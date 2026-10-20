/** A signed-in browser session: the bearer token the API verifies, plus a display label. */
export interface AuthSession {
  token: string;
  label: string;
  source: "dev" | "firebase";
}

/**
 * A way to obtain an ID token. The dev provider hands out pre-minted local tokens; the Firebase
 * provider (Week 6) will sign in with Firebase Auth and return its OIDC ID token.
 */
export interface AuthProvider {
  readonly kind: "dev" | "firebase";
  /** Whether this provider can be offered on the sign-in screen in this build. */
  readonly available: boolean;
  signIn(): Promise<AuthSession>;
}

export interface AuthState {
  session: AuthSession | null;
  signIn: (session: AuthSession) => void;
  signOut: (reason?: string) => void;
  /** Why the last session ended (expired token, sign-out), shown on the sign-in screen. */
  notice: string | null;
}
