import { render } from "@testing-library/react";
import App from "../App.tsx";
import type { AuthSession } from "../auth/types.ts";

export const TEST_TOKEN = "synthetic.test.token";

export function session(token = TEST_TOKEN): AuthSession {
  return { token, label: "Test lead", source: "dev" };
}

/** Renders the whole app signed in, at `path`, against the MSW mock API. */
export function renderApp(path: string, signedIn: AuthSession | null = session()) {
  return render(<App initialSession={signedIn} initialEntries={[path]} />);
}
