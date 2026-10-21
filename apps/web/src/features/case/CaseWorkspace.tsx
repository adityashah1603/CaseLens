import { ArrowLeft } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useCase } from "../../api/queries.ts";
import { AboutDemo } from "../../components/AboutDemo.tsx";
import { ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Badge } from "../../components/ui/badge.tsx";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs.tsx";
import { BROWSE_CASES } from "../../lib/nav.ts";
import { roleAtLeast } from "../../lib/roles.ts";
import { cn } from "../../lib/utils.ts";
import { AskPanel } from "../ask/AskPanel.tsx";
import { AuditPanel } from "../audit/AuditPanel.tsx";
import { EntitiesPanel } from "../entities/EntitiesPanel.tsx";
import { EvidencePanel } from "../evidence/EvidencePanel.tsx";
import { TimelinePanel } from "../timeline/TimelinePanel.tsx";
import { EvidenceViewer } from "../viewer/EvidenceViewer.tsx";
import { ViewerContext, type ViewerState, type ViewerTarget } from "../viewer/ViewerContext.ts";

const TABS = ["ask", "timeline", "entities", "evidence", "audit"] as const;
type Tab = (typeof TABS)[number];

function isTab(v: string | null): v is Tab {
  return v !== null && (TABS as readonly string[]).includes(v);
}

export function CaseWorkspace() {
  const { caseId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [target, setTarget] = useState<ViewerTarget | null>(null);
  const caseQuery = useCase(caseId);

  const tabParam = params.get("tab");
  const tab: Tab = isTab(tabParam) ? tabParam : "ask";
  const entityId = params.get("entity");

  const setTab = useCallback(
    (next: string) => {
      setParams(
        (p) => {
          const n = new URLSearchParams(p);
          n.set("tab", next);
          return n;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const setTimelineEntity = useCallback(
    (id: string | null, switchTab = false) => {
      setParams(
        (p) => {
          const n = new URLSearchParams(p);
          if (id === null) n.delete("entity");
          else n.set("entity", id);
          if (switchTab) n.set("tab", "timeline");
          return n;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const viewer = useMemo<ViewerState>(
    () => ({
      target,
      open: setTarget,
      close: () => {
        setTarget(null);
      },
    }),
    [target],
  );

  if (caseQuery.isPending) return <Loading label="Loading case" />;
  if (caseQuery.isError) {
    return (
      <main className="p-4">
        <ErrorBox error={caseQuery.error} />
        <Link to="/" state={BROWSE_CASES} className="text-sm text-sky-700 underline">
          Back to cases
        </Link>
      </main>
    );
  }
  const c = caseQuery.data;
  const role = c.my_role;
  const isLead = roleAtLeast(role, "lead");
  const canMutate = roleAtLeast(role, "analyst");

  return (
    <ViewerContext.Provider value={viewer}>
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white px-4 py-2">
          <Link to="/" state={BROWSE_CASES} className="inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900" aria-label="Back to cases">
            <ArrowLeft className="size-4" aria-hidden /> Cases
          </Link>
          <h1 className="text-base font-semibold text-slate-900">{c.name}</h1>
          <Badge tone="info" aria-label={`Your role: ${role}`}>
            {role}
          </Badge>
          {c.is_demo && <Badge tone="warning">demo case</Badge>}
          <span className="text-xs text-slate-500">{c.evidence_count} evidence files</span>
          {c.is_demo && (
            <div className="basis-full">
              <AboutDemo />
            </div>
          )}
        </div>
        <div className={cn("grid min-h-0 flex-1", target !== null && "lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]")}>
          <Tabs value={tab} onValueChange={setTab} className="min-h-0 bg-white">
            <TabsList aria-label="Case sections">
              <TabsTrigger value="ask">Ask</TabsTrigger>
              <TabsTrigger value="timeline">Timeline</TabsTrigger>
              <TabsTrigger value="entities">Entities</TabsTrigger>
              <TabsTrigger value="evidence">Evidence</TabsTrigger>
              {isLead && <TabsTrigger value="audit">Audit</TabsTrigger>}
            </TabsList>
            <TabsContent value="ask">
              <AskPanel caseData={c} />
            </TabsContent>
            <TabsContent value="timeline">
              <TimelinePanel
                caseId={c.id}
                entityId={entityId}
                onEntityChange={(id) => {
                  setTimelineEntity(id);
                }}
              />
            </TabsContent>
            <TabsContent value="entities">
              <EntitiesPanel
                caseId={c.id}
                canRevert={canMutate}
                onShowTimeline={(id) => {
                  setTimelineEntity(id, true);
                }}
              />
            </TabsContent>
            <TabsContent value="evidence">
              <EvidencePanel caseId={c.id} canUpload={canMutate} />
            </TabsContent>
            {isLead && (
              <TabsContent value="audit">
                <AuditPanel caseId={c.id} isLead={isLead} />
              </TabsContent>
            )}
          </Tabs>
          {target !== null && (
            <div className="min-h-[60vh] border-t border-slate-200 lg:min-h-0 lg:border-t-0 lg:border-l">
              <EvidenceViewer
                caseId={c.id}
                target={target}
                onClose={() => {
                  setTarget(null);
                }}
              />
            </div>
          )}
        </div>
      </div>
    </ViewerContext.Provider>
  );
}
