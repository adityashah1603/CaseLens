import type { AuthProvider, AuthSession } from "./types.ts";

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

type FirebaseEnv = Partial<
  Record<
    "VITE_FIREBASE_API_KEY" | "VITE_FIREBASE_AUTH_DOMAIN" | "VITE_FIREBASE_PROJECT_ID" | "VITE_FIREBASE_APP_ID",
    string | undefined
  >
>;

/**
 * The `VITE_FIREBASE_*` values, read one by one: passing `import.meta.env` as a whole object would
 * make Vite inline every `VITE_*` value (dev tokens included) into the production bundle.
 */
function firebaseEnv(): FirebaseEnv {
  return {
    VITE_FIREBASE_API_KEY: import.meta.env.VITE_FIREBASE_API_KEY,
    VITE_FIREBASE_AUTH_DOMAIN: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
    VITE_FIREBASE_PROJECT_ID: import.meta.env.VITE_FIREBASE_PROJECT_ID,
    VITE_FIREBASE_APP_ID: import.meta.env.VITE_FIREBASE_APP_ID,
  };
}

/** The Firebase web config, or null unless every `VITE_FIREBASE_*` value is set. */
export function firebaseConfig(env: FirebaseEnv = firebaseEnv()): FirebaseWebConfig | null {
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
