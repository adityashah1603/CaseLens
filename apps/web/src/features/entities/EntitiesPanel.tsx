import { CalendarRange, Undo2 } from "lucide-react";
import { useState } from "react";
import { useEntities, useEntity, useGraph, useRevertLink } from "../../api/queries.ts";
import type { EntityLink, EntityType } from "../../api/types.ts";
import { Empty, ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Badge } from "../../components/ui/badge.tsx";
import { Button } from "../../components/ui/button.tsx";
import { Input, Label, Select } from "../../components/ui/input.tsx";
import { formatTs, shortId } from "../../lib/format.ts";
import { cn } from "../../lib/utils.ts";
import { SourceButton } from "../viewer/SourceButton.tsx";
import { TYPE_COLOR } from "./colors.ts";
import { GraphView } from "./GraphView.tsx";

const TYPES: EntityType[] = ["person", "vehicle", "phone", "location", "weapon"];

function RevertButton({ caseId, link }: { caseId: string; link: EntityLink }) {
  const revert = useRevertLink(caseId);
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <Button
        variant="secondary"
        size="sm"
        onClick={() => {
          setConfirming(true);
        }}
      >
        <Undo2 aria-hidden /> Revert merge
      </Button>
    );
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Button
        variant="destructive"
        size="sm"
        disabled={revert.isPending}
        onClick={() => {
          revert.mutate(link.id, {
            onSuccess: () => {
              setConfirming(false);
            },
          });
        }}
      >
        Confirm revert
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          setConfirming(false);
          revert.reset();
        }}
      >
        Cancel
      </Button>
      {revert.isError && <span className="text-xs text-red-700">{revert.error.message}</span>}
    </span>
  );
}

function EntityDetailView({
  caseId,
  entityId,
  canRevert,
  labels,
  onSelect,
  onShowTimeline,
}: {
  caseId: string;
  entityId: string;
  canRevert: boolean;
  labels: Map<string, string>;
  onSelect: (id: string) => void;
  onShowTimeline: (id: string) => void;
}) {
  const detail = useEntity(caseId, entityId);
  if (detail.isPending) return <Loading label="Loading entity" />;
  if (detail.isError) return <ErrorBox error={detail.error} />;
  const e = detail.data;
  const attrs = Object.entries(e.attributes);

  return (
    <article aria-label={`Entity ${e.canonical_name}`} className="flex flex-col gap-3">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-slate-900">{e.canonical_name}</h3>
          <p className="text-xs text-slate-500">
            {e.type}
            {e.normalized_key !== null && ` · ${e.normalized_key}`} · {e.mention_count} mentions
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            onShowTimeline(e.id);
          }}
        >
          <CalendarRange aria-hidden /> Show on timeline
        </Button>
      </header>
      {e.aliases.length > 0 && (
        <p className="text-xs text-slate-600">
          <span className="font-medium">Also known as:</span> {e.aliases.join(", ")}
        </p>
      )}
      {attrs.length > 0 && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
          {attrs.map(([k, v]) => (
            <div key={k} className="flex gap-1">
              <dt className="text-slate-500">{k}:</dt>
              <dd className="text-slate-800">{typeof v === "string" || typeof v === "number" ? String(v) : JSON.stringify(v)}</dd>
            </div>
          ))}
        </dl>
      )}

      <section aria-label="Merge links">
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Linked entities</h4>
        {e.links.length === 0 ? (
          <p className="text-sm text-slate-500">No merges.</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {e.links.map((l) => {
              const otherId = l.from_entity_id === e.id ? l.to_entity_id : l.from_entity_id;
              return (
                <li key={l.id} className="flex flex-wrap items-center gap-2 rounded border border-slate-200 p-2 text-sm" data-testid="entity-link">
                  <button
                    type="button"
                    className="font-medium text-sky-800 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
                    onClick={() => {
                      onSelect(otherId);
                    }}
                  >
                    {labels.get(otherId) ?? `Entity ${shortId(otherId)}`}
                  </button>
                  <Badge>{l.method}</Badge>
                  {l.confidence !== null && <span className="text-xs text-slate-500">{Math.round(l.confidence * 100)}%</span>}
                  {l.status === "reverted" ? (
                    <Badge tone="warning">reverted {formatTs(l.reverted_at)}</Badge>
                  ) : canRevert ? (
                    <span className="ml-auto">
                      <RevertButton caseId={caseId} link={l} />
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-label="Mentions">
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Mentions</h4>
        {e.mentions.length === 0 ? (
          <p className="text-sm text-slate-500">No mentions.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {e.mentions.map((m) => (
              <li key={m.id} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-slate-800">"{m.surface_text}"</span>
                {m.needs_review && <Badge tone="warning">needs review</Badge>}
                <SourceButton target={{ evidenceId: m.evidence_id, locator: m.locator, quote: m.surface_text, origin: "Mention" }} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  );
}

export function EntitiesPanel({
  caseId,
  canRevert,
  onShowTimeline,
}: {
  caseId: string;
  canRevert: boolean;
  onShowTimeline: (entityId: string) => void;
}) {
  const [q, setQ] = useState("");
  const [type, setType] = useState<EntityType | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<"list" | "graph">("list");
  const entities = useEntities(caseId, type, q);
  const graph = useGraph(caseId);
  const labels = new Map<string, string>();
  for (const n of graph.data?.nodes ?? []) labels.set(n.id, n.label);
  for (const en of entities.data?.items ?? []) labels.set(en.id, en.canonical_name);

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-48 flex-1 flex-col gap-1">
          <Label htmlFor="ent-q">Search entities</Label>
          <Input
            id="ent-q"
            type="search"
            value={q}
            maxLength={200}
            placeholder="Name, plate or phone"
            onChange={(e) => {
              setQ(e.target.value);
            }}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="ent-type">Type</Label>
          <Select
            id="ent-type"
            value={type ?? ""}
            onChange={(e) => {
              setType(e.target.value === "" ? null : (e.target.value as EntityType));
            }}
          >
            <option value="">All types</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex gap-1" role="group" aria-label="View">
          <Button
            variant={mode === "list" ? "default" : "secondary"}
            aria-pressed={mode === "list"}
            onClick={() => {
              setMode("list");
            }}
          >
            List
          </Button>
          <Button
            variant={mode === "graph" ? "default" : "secondary"}
            aria-pressed={mode === "graph"}
            onClick={() => {
              setMode("graph");
            }}
          >
            Graph
          </Button>
        </div>
      </div>

      <div className={cn("grid gap-4", mode === "list" ? "lg:grid-cols-[minmax(14rem,18rem)_1fr]" : "grid-cols-1")}>
        {mode === "list" ? (
          <div className="min-w-0">
            {entities.isPending && <Loading />}
            {entities.isError && <ErrorBox error={entities.error} />}
            {entities.data?.items.length === 0 && <Empty>No entities match.</Empty>}
            <ul className="flex max-h-[60vh] flex-col overflow-auto" aria-label="Entities">
              {entities.data?.items.map((en) => (
                <li key={en.id}>
                  <button
                    type="button"
                    aria-current={en.id === selected ? "true" : undefined}
                    onClick={() => {
                      setSelected(en.id);
                    }}
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 aria-[current=true]:bg-slate-100"
                  >
                    <span className="size-2.5 shrink-0 rounded-full" style={{ background: TYPE_COLOR[en.type] }} aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{en.canonical_name}</span>
                    <span className="text-xs tabular-nums text-slate-500">{en.mention_count}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="min-w-0">
            {graph.isPending && <Loading label="Loading graph" />}
            {graph.isError && <ErrorBox error={graph.error} />}
            {graph.data !== undefined && <GraphView graph={graph.data} selectedId={selected} onSelect={setSelected} />}
          </div>
        )}
        <div className="min-w-0">
          {selected === null ? (
            <Empty>Select an entity to see its mentions and merges.</Empty>
          ) : (
            <EntityDetailView
              caseId={caseId}
              entityId={selected}
              canRevert={canRevert}
              labels={labels}
              onSelect={setSelected}
              onShowTimeline={onShowTimeline}
            />
          )}
        </div>
      </div>
    </div>
  );
}
