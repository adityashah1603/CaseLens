import { KeyRound, Loader2, LogIn } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { AboutDemoText } from "../components/AboutDemo.tsx";
import { Button } from "../components/ui/button.tsx";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card.tsx";
import { Label, Textarea } from "../components/ui/input.tsx";
import { useAuth } from "./AuthContext.ts";
import { devIdentities, devSession, devSignInEnabled } from "./dev.ts";
import { firebaseProvider } from "./firebase.ts";

export function SignInPage() {
  const { signIn, notice } = useAuth();
  const firebase = firebaseProvider();
  const [pending, setPending] = useState(false);
  const [pasted, setPasted] = useState("");
  const [firebaseError, setFirebaseError] = useState<string | null>(null);
  const devEnabled = devSignInEnabled();
  const identities = devIdentities();

  const onPaste = (e: SubmitEvent) => {
    e.preventDefault();
    if (pasted.trim() === "") return;
    signIn(devSession(pasted, "Pasted token"));
  };

  return (
    <main className="mx-auto flex w-full max-w-xl flex-col gap-4 p-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Sign in to CaseLens</h1>
        <p className="text-sm text-slate-600">Evidence search with cited, auditable answers.</p>
      </div>
      {notice !== null && (
        <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 p-2 text-sm text-amber-950">
          {notice}
        </p>
      )}

      {firebase.available && (
        <Card>
          <CardHeader>
            <CardTitle>Try the demo</CardTitle>
            <p className="text-sm text-slate-600">
              All data here is <strong>SYNTHETIC</strong>. You sign in anonymously as a read-only viewer of the demo
              case: you can ask questions and open every citation, but not upload or change anything.
            </p>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div>
              <Button
                size="lg"
                disabled={pending}
                onClick={() => {
                  setPending(true);
                  setFirebaseError(null);
                  firebase.signIn().then(signIn, (e: unknown) => {
                    setPending(false);
                    setFirebaseError(
                      e instanceof Error ? `Demo sign-in failed: ${e.message}` : "Demo sign-in failed. Try again.",
                    );
                  });
                }}
              >
                {pending ? <Loader2 className="animate-spin" aria-hidden /> : <LogIn aria-hidden />}
                {pending ? "Signing in" : "Try the demo (read-only)"}
              </Button>
            </div>
            {firebaseError !== null && (
              <p role="alert" className="text-sm text-red-800">
                {firebaseError}
              </p>
            )}
            <div className="border-t border-slate-100 pt-3">
              <h2 className="mb-1.5 text-sm font-semibold text-slate-900">About this demo</h2>
              <AboutDemoText />
            </div>
          </CardContent>
        </Card>
      )}

      {devEnabled ? (
        <Card>
          <CardHeader>
            <CardTitle>Development sign-in</CardTitle>
            <p className="text-xs text-slate-500">
              Local tokens minted by <code>python -m caselens_api seed-dev-users</code>, read from{" "}
              <code>apps/web/.env.local</code>. Not available in production builds.
            </p>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <ul className="flex flex-col gap-2" aria-label="Dev identities">
              {identities.map((id) => (
                <li key={id.key} className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-slate-900">{id.label}</p>
                    <p className="text-xs text-slate-500">{id.description}</p>
                  </div>
                  <Button
                    variant="secondary"
                    disabled={id.token === undefined}
                    title={id.token === undefined ? `Set VITE_DEV_TOKEN_${id.key.toUpperCase()} in .env.local` : undefined}
                    onClick={() => {
                      if (id.token !== undefined) signIn(devSession(id.token, id.label));
                    }}
                  >
                    Sign in as {id.key}
                  </Button>
                </li>
              ))}
            </ul>
            <form onSubmit={onPaste} className="flex flex-col gap-1.5">
              <Label htmlFor="pasted-token">Or paste a token</Label>
              <Textarea
                id="pasted-token"
                className="font-mono text-xs"
                autoComplete="off"
                spellCheck={false}
                value={pasted}
                onChange={(e) => {
                  setPasted(e.target.value);
                }}
              />
              <div>
                <Button type="submit" disabled={pasted.trim() === ""}>
                  <KeyRound aria-hidden /> Use token
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      ) : (
        !firebase.available && (
          <p className="text-sm text-slate-600">Sign-in is not configured for this deployment.</p>
        )
      )}
    </main>
  );
}
