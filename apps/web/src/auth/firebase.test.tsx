import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { me } from "../test/fixtures.ts";
import { renderApp } from "../test/render.tsx";
import { API, mockState, server } from "../test/server.ts";
import { firebaseConfig, FirebaseAuthProvider, setFirebaseProviderForTests, type FirebaseWebConfig } from "./firebase.ts";

// A fake Firebase SDK: anonymous users whose ID token changes on a forced refresh. SYNTHETIC values.
const fb = vi.hoisted(() => {
  const state = { tokenVersion: 1, signedIn: false };
  const user = {
    getIdToken: (force?: boolean) => {
      if (force === true) state.tokenVersion += 1;
      return Promise.resolve(`synthetic-id-token-${String(state.tokenVersion)}`);
    },
  };
  const auth = {
    get currentUser() {
      return state.signedIn ? user : null;
    },
    authStateReady: () => Promise.resolve(),
    signOut: vi.fn(() => {
      state.signedIn = false;
      return Promise.resolve();
    }),
  };
  return {
    state,
    auth,
    initializeApp: vi.fn((config: unknown) => ({ config })),
    getAuth: vi.fn(() => auth),
    setPersistence: vi.fn(() => Promise.resolve()),
    signInAnonymously: vi.fn(() => {
      state.signedIn = true;
      return Promise.resolve({ user });
    }),
    browserSessionPersistence: { type: "SESSION" },
  };
});

vi.mock("firebase/app", () => ({ initializeApp: fb.initializeApp }));
vi.mock("firebase/auth", () => ({
  getAuth: fb.getAuth,
  setPersistence: fb.setPersistence,
  signInAnonymously: fb.signInAnonymously,
  browserSessionPersistence: fb.browserSessionPersistence,
}));

const CONFIG: FirebaseWebConfig = {
  apiKey: "SYNTHETIC-api-key",
  authDomain: "synthetic-demo.firebaseapp.test",
  projectId: "synthetic-demo",
  appId: "1:000000000000:web:synthetic",
};

function demoMe() {
  const base = me(false);
  return { ...base, user: { ...base.user, email: null, display_name: "Demo viewer", is_demo: true } };
}

beforeEach(() => {
  fb.state.tokenVersion = 1;
  fb.state.signedIn = false;
  vi.clearAllMocks();
  mockState.role = "viewer";
  setFirebaseProviderForTests(new FirebaseAuthProvider(CONFIG));
  server.use(http.get(`${API}/v1/me`, () => HttpResponse.json(demoMe())));
});

afterEach(() => {
  setFirebaseProviderForTests(null);
});

describe("Firebase config", () => {
  it("is null unless every value is set", () => {
    expect(firebaseConfig({})).toBeNull();
    expect(
      firebaseConfig({
        VITE_FIREBASE_API_KEY: "k",
        VITE_FIREBASE_AUTH_DOMAIN: " ",
        VITE_FIREBASE_PROJECT_ID: "p",
        VITE_FIREBASE_APP_ID: "a",
      }),
    ).toBeNull();
    expect(
      firebaseConfig({
        VITE_FIREBASE_API_KEY: CONFIG.apiKey,
        VITE_FIREBASE_AUTH_DOMAIN: CONFIG.authDomain,
        VITE_FIREBASE_PROJECT_ID: CONFIG.projectId,
        VITE_FIREBASE_APP_ID: CONFIG.appId,
      }),
    ).toEqual(CONFIG);
  });

  it("hides the demo button when Firebase is not configured", () => {
    setFirebaseProviderForTests(new FirebaseAuthProvider(null));
    renderApp("/", null);
    expect(screen.queryByRole("button", { name: "Try the demo (read-only)" })).not.toBeInTheDocument();
  });
});

describe("FirebaseAuthProvider", () => {
  it("signs in anonymously with session persistence and returns the ID token", async () => {
    const provider = new FirebaseAuthProvider(CONFIG);
    expect(fb.initializeApp).not.toHaveBeenCalled();
    const session = await provider.signIn();
    expect(session).toEqual({ token: "synthetic-id-token-1", label: "Demo viewer", source: "firebase" });
    expect(fb.initializeApp).toHaveBeenCalledExactlyOnceWith(CONFIG);
    expect(fb.setPersistence).toHaveBeenCalledWith(fb.auth, fb.browserSessionPersistence);
    expect(fb.signInAnonymously).toHaveBeenCalledOnce();
    expect(await provider.getIdToken()).toBe("synthetic-id-token-1");
    expect(await provider.getIdToken(true)).toBe("synthetic-id-token-2");
    await provider.signOut();
    expect(fb.auth.signOut).toHaveBeenCalledOnce();
    expect(await provider.getIdToken()).toBeNull();
  });

  it("does not load the SDK to sign out when it was never used", async () => {
    await new FirebaseAuthProvider(CONFIG).signOut();
    expect(fb.initializeApp).not.toHaveBeenCalled();
  });
});

describe("demo sign-in flow", () => {
  it("signs in anonymously, opens the demo case and sends the ID token", async () => {
    const user = userEvent.setup();
    renderApp("/", null);
    expect(screen.getByText(/read-only viewer of the demo/)).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Synthetic data notice" })).toHaveTextContent("SYNTHETIC DATA");
    await user.click(screen.getByRole("button", { name: "Try the demo (read-only)" }));
    // Straight into the only case, without stopping at the case list.
    expect(await screen.findByRole("heading", { name: /Riverside Lot Homicide/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your cases" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Who was in the gray sedan on March 14?" })).toBeInTheDocument();
    expect(screen.getByTestId("about-demo")).toHaveTextContent("10 to 60 seconds");
    expect(fb.signInAnonymously).toHaveBeenCalledOnce();
    expect(mockState.authHeaders.length).toBeGreaterThan(0);
    expect(new Set(mockState.authHeaders)).toEqual(new Set(["Bearer synthetic-id-token-1"]));
  });

  it("the Cases link still shows the list", async () => {
    const user = userEvent.setup();
    renderApp("/", null);
    await user.click(screen.getByRole("button", { name: "Try the demo (read-only)" }));
    await screen.findByRole("heading", { name: /Riverside Lot Homicide/ });
    await user.click(screen.getByRole("link", { name: "Cases" }));
    expect(await screen.findByRole("heading", { name: "Your cases" })).toBeInTheDocument();
  });

  it("retries once with a refreshed ID token after a 401", async () => {
    const seen: (string | null)[] = [];
    server.use(
      http.get(`${API}/v1/me`, ({ request }) => {
        const auth = request.headers.get("Authorization");
        seen.push(auth);
        if (auth !== "Bearer synthetic-id-token-2") {
          return HttpResponse.json({ title: "Unauthorized", status: 401, detail: "Token expired." }, { status: 401 });
        }
        return HttpResponse.json(demoMe());
      }),
    );
    const user = userEvent.setup();
    renderApp("/", null);
    await user.click(screen.getByRole("button", { name: "Try the demo (read-only)" }));
    expect(await screen.findByText("Demo viewer")).toBeInTheDocument();
    expect(seen.slice(0, 2)).toEqual(["Bearer synthetic-id-token-1", "Bearer synthetic-id-token-2"]);
    expect(screen.queryByRole("heading", { name: "Sign in to CaseLens" })).not.toBeInTheDocument();
  });

  it("signs out of Firebase and returns to the sign-in screen", async () => {
    const user = userEvent.setup();
    renderApp("/", null);
    await user.click(screen.getByRole("button", { name: "Try the demo (read-only)" }));
    await screen.findByRole("heading", { name: /Riverside Lot Homicide/ });
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Try the demo (read-only)" })).toBeInTheDocument();
    await waitFor(() => {
      expect(fb.auth.signOut).toHaveBeenCalledOnce();
    });
    expect(sessionStorage.getItem("caselens.session")).toBeNull();
  });

  it("shows an error when anonymous sign-in fails", async () => {
    fb.signInAnonymously.mockRejectedValueOnce(new Error("auth/operation-not-allowed"));
    const user = userEvent.setup();
    renderApp("/", null);
    await user.click(screen.getByRole("button", { name: "Try the demo (read-only)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Demo sign-in failed: auth/operation-not-allowed");
    expect(screen.getByRole("button", { name: "Try the demo (read-only)" })).toBeEnabled();
  });
});
