import type { AuthProvider, AuthSession } from "./types.ts";

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

type FirebaseEnv = Pick<
  ImportMetaEnv,
  "VITE_FIREBASE_API_KEY" | "VITE_FIREBASE_AUTH_DOMAIN" | "VITE_FIREBASE_PROJECT_ID" | "VITE_FIREBASE_APP_ID"
>;

/** The Firebase web config, or null unless every `VITE_FIREBASE_*` value is set. */
export function firebaseConfig(env: FirebaseEnv = import.meta.env): FirebaseWebConfig | null {
  const apiKey = env.VITE_FIREBASE_API_KEY;
  const authDomain = env.VITE_FIREBASE_AUTH_DOMAIN;
  const projectId = env.VITE_FIREBASE_PROJECT_ID;
  const appId = env.VITE_FIREBASE_APP_ID;
  if (!apiKey || !authDomain || !projectId || !appId) return null;
  return { apiKey, authDomain, projectId, appId };
}

/**
 * Firebase Auth provider stub. It is offered only when the Firebase config exists. The SDK is not
 * bundled yet: Week 6 adds `firebase`, calls `signInWithPopup` (Google) or the demo credentials,
 * and returns `user.getIdToken()` (refreshed before its ~1 h expiry) as the bearer token.
 */
export class FirebaseAuthProvider implements AuthProvider {
  readonly kind = "firebase" as const;
  readonly available: boolean;

  constructor(readonly config: FirebaseWebConfig | null = firebaseConfig()) {
    this.available = config !== null;
  }

  signIn(): Promise<AuthSession> {
    return Promise.reject(
      new Error("Firebase sign-in is not wired in this build yet (planned for the deployment week)."),
    );
  }
}
