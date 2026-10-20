import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LogOut, ScanSearch } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { BrowserRouter, Link, MemoryRouter, Route, Routes } from "react-router";
import { ApiContext } from "./api/ApiContext.ts";
import { ApiError, createApiConnection } from "./api/client.ts";
import { useMe } from "./api/queries.ts";
import { useAuth } from "./auth/AuthContext.ts";
import { SessionProvider } from "./auth/SessionProvider.tsx";
import { SignInPage } from "./auth/SignInPage.tsx";
import type { AuthSession } from "./auth/types.ts";
import { Empty } from "./components/Feedback.tsx";
import { SyntheticBanner } from "./components/SyntheticBanner.tsx";
import { Button } from "./components/ui/button.tsx";
import { AdminPage } from "./features/admin/AdminPage.tsx";
import { CaseWorkspace } from "./features/case/CaseWorkspace.tsx";
import { CasesPage } from "./features/cases/CasesPage.tsx";

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
      },
      mutations: { retry: false },
    },
  });
}

function TopBar() {
  const { session, signOut } = useAuth();
  const me = useMe();
  return (
    <header className="flex shrink-0 items-center gap-4 border-b border-slate-800 bg-slate-900 px-4 py-2 text-slate-100">
      <Link to="/" className="flex items-center gap-1.5 font-semibold tracking-tight">
        <ScanSearch className="size-4" aria-hidden /> CaseLens
      </Link>
      <nav aria-label="Main" className="flex gap-3 text-sm text-slate-300">
        <Link to="/" className="hover:text-white">
          Cases
        </Link>
        {me.data?.user.is_admin === true && (
          <Link to="/admin" className="hover:text-white">
            Admin
          </Link>
        )}
      </nav>
      <div className="ml-auto flex items-center gap-2 text-xs text-slate-300">
        <span>{me.data?.user.display_name ?? me.data?.user.email ?? session?.label}</span>
        <Button
          variant="ghost"
          size="sm"
          className="text-slate-200 hover:bg-slate-800 hover:text-white"
          onClick={() => {
            signOut();
          }}
        >
          <LogOut aria-hidden /> Sign out
        </Button>
      </div>
    </header>
  );
}

function SignedIn({ session, children }: { session: AuthSession; children: ReactNode }) {
  const { signOut } = useAuth();
  const token = session.token;
  const api = useMemo(
    () =>
      createApiConnection({
        getToken: () => token,
        onUnauthorized: () => {
          signOut("The API rejected the token (expired or invalid). Sign in again.");
        },
      }),
    [token, signOut],
  );
  const queryClient = useMemo(() => makeQueryClient(), []);
  return (
    <ApiContext.Provider value={api}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </ApiContext.Provider>
  );
}

function AppRoutes() {
  return (
    <>
      <TopBar />
      <div className="flex min-h-0 flex-1 flex-col overflow-auto">
        <Routes>
          <Route path="/" element={<CasesPage />} />
          <Route path="/cases/:caseId" element={<CaseWorkspace />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<Empty>Page not found.</Empty>} />
        </Routes>
      </div>
    </>
  );
}

function Shell({ initialEntries }: { initialEntries: string[] | undefined }) {
  const { session } = useAuth();
  const Router = initialEntries === undefined ? BrowserRouter : MemoryRouter;
  return (
    <div className="flex h-dvh flex-col bg-slate-50 text-slate-900">
      <SyntheticBanner />
      {session === null ? (
        <div className="flex-1 overflow-auto">
          <SignInPage />
        </div>
      ) : (
        <SignedIn key={session.token} session={session}>
          <Router {...(initialEntries === undefined ? {} : { initialEntries })}>
            <AppRoutes />
          </Router>
        </SignedIn>
      )}
    </div>
  );
}

export default function App({
  initialSession,
  initialEntries,
}: {
  /** Tests only: start signed in (or explicitly signed out with null). */
  initialSession?: AuthSession | null;
  /** Tests only: use an in-memory router at these entries. */
  initialEntries?: string[];
}) {
  return (
    <SessionProvider {...(initialSession === undefined ? {} : { initial: initialSession })}>
      <Shell initialEntries={initialEntries} />
    </SessionProvider>
  );
}
