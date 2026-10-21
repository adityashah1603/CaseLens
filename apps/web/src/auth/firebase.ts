import type { Auth } from "firebase/auth";
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
  const apiKey = env.VITE_FIREBASE_API_KEY?.trim();
  const authDomain = env.VITE_FIREBASE_AUTH_DOMAIN?.trim();
  const projectId = env.VITE_FIREBASE_PROJECT_ID?.trim();
  const appId = env.VITE_FIREBASE_APP_ID?.trim();
  if (!apiKey || !authDomain || !projectId || !appId) return null;
  return { apiKey, authDomain, projectId, appId };
}

export const DEMO_LABEL = "Demo viewer";

/**
 * Firebase Auth for the public demo: anonymous sign-in, which the API maps to the shared read-only
 * demo viewer. The SDK (`firebase/app` + `firebase/auth`) is imported on first use only, so it is
 * a separate chunk that never loads on the first paint (and never at all without the config).
 * The session persists in `sessionStorage` (`browserSessionPersistence`): it ends with the tab.
 */
export class FirebaseAuthProvider implements AuthProvider {
  readonly kind = "firebase" as const;
  readonly available: boolean;
  private auth: Promise<Auth> | null = null;

  constructor(readonly config: FirebaseWebConfig | null = firebaseConfig()) {
    this.available = config !== null;
  }

  private load(): Promise<Auth> {
    const config = this.config;
    if (config === null) return Promise.reject(new Error("Firebase sign-in is not configured for this deployment."));
    this.auth ??= (async () => {
      const [{ initializeApp }, { getAuth, setPersistence, browserSessionPersistence }] = await Promise.all([
        import("firebase/app"),
        import("firebase/auth"),
      ]);
      const auth = getAuth(initializeApp(config));
      await setPersistence(auth, browserSessionPersistence);
      return auth;
    })().catch((e: unknown) => {
      this.auth = null;
      throw e;
    });
    return this.auth;
  }

  /** Signs in anonymously and returns the first ID token as the session token. */
  async signIn(): Promise<AuthSession> {
    const auth = await this.load();
    const { signInAnonymously } = await import("firebase/auth");
    const credential = await signInAnonymously(auth);
    const token = await credential.user.getIdToken();
    return { token, label: DEMO_LABEL, source: "firebase" };
  }

  /**
   * The current ID token. The SDK caches it and refreshes it near its ~1 h expiry; `forceRefresh`
   * mints a new one (used once after a 401). After a reload the SDK restores the user from
   * `sessionStorage` asynchronously, so this waits for that first. Null when nobody is signed in.
   */
  async getIdToken(forceRefresh = false): Promise<string | null> {
    if (!this.available) return null;
    const auth = await this.load();
    await auth.authStateReady();
    const user = auth.currentUser;
    return user === null ? null : user.getIdToken(forceRefresh);
  }

  /** Signs out of Firebase, if the SDK was ever loaded in this tab. */
  async signOut(): Promise<void> {
    if (this.auth === null) return;
    const auth = await this.auth.catch(() => null);
    await auth?.signOut();
  }
}

let shared: FirebaseAuthProvider | null = null;

/** The app-wide Firebase provider (one Firebase app per tab). */
export function firebaseProvider(): FirebaseAuthProvider {
  shared ??= new FirebaseAuthProvider();
  return shared;
}

/** Tests only: replace (or reset with null) the app-wide provider. */
export function setFirebaseProviderForTests(provider: FirebaseAuthProvider | null): void {
  shared = provider;
}
