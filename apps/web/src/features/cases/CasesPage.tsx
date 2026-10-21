import { FolderOpen } from "lucide-react";
import { Link, Navigate, useLocation } from "react-router";
import { useCases, useMe } from "../../api/queries.ts";
import { useAuth } from "../../auth/AuthContext.ts";
import type { Case } from "../../api/types.ts";
import { Empty, ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Badge } from "../../components/ui/badge.tsx";
import { formatTs } from "../../lib/format.ts";
import { isBrowsing } from "../../lib/nav.ts";

function IngestStatus({ c }: { c: Case }) {
  const s = c.ingest_summary;
  const total = s.queued + s.running + s.succeeded + s.failed + s.dead_lettered;
  if (total === 0) return <span className="text-xs text-slate-500">no ingest jobs</span>;
  if (s.dead_lettered > 0) return <Badge tone="danger">{s.dead_lettered} failed stages</Badge>;
  if (s.queued + s.running > 0) return <Badge tone="info">ingesting: {s.succeeded}/{total} stages</Badge>;
  return <Badge tone="success">ingested</Badge>;
}

export function CasesPage() {
  const cases = useCases();
  const me = useMe();
  const { session } = useAuth();
  const location = useLocation();
  const items = cases.data?.items;
  // A demo visitor (anonymous Firebase sign-in, or the API's demo user) has just the demo case:
  // open it straight away, unless they navigated back to the list on purpose.
  const demoVisitor = session?.source === "firebase" || me.data?.user.is_demo === true;
  const only = items?.length === 1 ? items[0] : undefined;
  if (demoVisitor && only !== undefined && !isBrowsing(location.state)) {
    return <Navigate to={`/cases/${only.id}`} replace />;
  }
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-4 p-4">
      <h1 className="text-lg font-semibold text-slate-900">Your cases</h1>
      {cases.isPending && <Loading label="Loading cases" />}
      {cases.isError && <ErrorBox error={cases.error} />}
      {cases.data?.items.length === 0 && <Empty>You are not a member of any case yet.</Empty>}
      <ul className="grid gap-3 md:grid-cols-2" aria-label="Cases">
        {cases.data?.items.map((c) => (
          <li key={c.id}>
            <Link
              to={`/cases/${c.id}`}
              className="flex h-full flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4 hover:border-slate-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="flex items-center gap-2 font-semibold text-slate-900">
                  <FolderOpen className="size-4 text-slate-500" aria-hidden />
                  {c.name}
                </span>
                <Badge>{c.my_role}</Badge>
              </div>
              {c.description !== null && <p className="text-sm text-slate-600">{c.description}</p>}
              <div className="mt-auto flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span>{c.evidence_count} evidence files</span>
                <IngestStatus c={c} />
                {c.is_demo && <Badge tone="warning">demo</Badge>}
                <Badge>{c.status}</Badge>
                {c.last_activity_at !== null && <span>last activity {formatTs(c.last_activity_at)}</span>}
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
