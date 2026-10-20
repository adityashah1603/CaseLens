import { useState, type SubmitEvent } from "react";
import { useEntities, useTimeline, type TimelineFilter } from "../../api/queries.ts";
import { Empty, ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Badge } from "../../components/ui/badge.tsx";
import { Button } from "../../components/ui/button.tsx";
import { Input, Label, Select } from "../../components/ui/input.tsx";
import { formatTs } from "../../lib/format.ts";
import { SourceButton } from "../viewer/SourceButton.tsx";

/** `datetime-local` value (viewer's zone) to ISO-8601, or undefined when blank. */
function toIso(local: string): string | undefined {
  if (local === "") return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export function TimelinePanel({
  caseId,
  entityId,
  onEntityChange,
}: {
  caseId: string;
  entityId: string | null;
  onEntityChange: (id: string | null) => void;
}) {
  const [fromLocal, setFromLocal] = useState("");
  const [toLocal, setToLocal] = useState("");
  const [span, setSpan] = useState<{ from?: string; to?: string }>({});
  const entities = useEntities(caseId, null, "");
  const filter: TimelineFilter = { ...span, ...(entityId === null ? {} : { entity_id: entityId }) };
  const timeline = useTimeline(caseId, filter);

  const apply = (e: SubmitEvent) => {
    e.preventDefault();
    const from = toIso(fromLocal);
    const to = toIso(toLocal);
    setSpan({ ...(from === undefined ? {} : { from }), ...(to === undefined ? {} : { to }) });
  };

  const events = timeline.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="flex flex-col gap-3 p-4">
      <form onSubmit={apply} className="flex flex-wrap items-end gap-2" aria-label="Timeline filters">
        <div className="flex min-w-48 flex-col gap-1">
          <Label htmlFor="tl-entity">Entity</Label>
          <Select
            id="tl-entity"
            value={entityId ?? ""}
            onChange={(e) => {
              onEntityChange(e.target.value === "" ? null : e.target.value);
            }}
          >
            <option value="">All entities</option>
            {entities.data?.items.map((en) => (
              <option key={en.id} value={en.id}>
                {en.canonical_name} ({en.type})
              </option>
            ))}
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="tl-from">From (your time zone)</Label>
          <Input
            id="tl-from"
            type="datetime-local"
            value={fromLocal}
            onChange={(e) => {
              setFromLocal(e.target.value);
            }}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="tl-to">To</Label>
          <Input
            id="tl-to"
            type="datetime-local"
            value={toLocal}
            onChange={(e) => {
              setToLocal(e.target.value);
            }}
          />
        </div>
        <Button type="submit" variant="secondary">
          Apply
        </Button>
        {(span.from !== undefined || span.to !== undefined) && (
          <Button
            variant="ghost"
            onClick={() => {
              setFromLocal("");
              setToLocal("");
              setSpan({});
            }}
          >
            Clear window
          </Button>
        )}
      </form>

      {timeline.isPending && <Loading label="Loading timeline" />}
      {timeline.isError && <ErrorBox error={timeline.error} />}
      {timeline.isSuccess && events.length === 0 && <Empty>No events match these filters.</Empty>}

      <ol className="relative ml-2 flex flex-col gap-3 border-l-2 border-slate-200 pl-4" aria-label="Timeline events">
        {events.map((ev) => (
          <li key={ev.id} className="relative" data-testid="timeline-event">
            <span className="absolute -left-[23px] top-1.5 size-3 rounded-full border-2 border-white bg-slate-700" aria-hidden />
            <p className="text-xs font-medium tabular-nums text-slate-500">
              {formatTs(ev.t_start)}
              {ev.t_end !== null && ` to ${formatTs(ev.t_end)}`}
              {ev.location !== null && <span className="text-slate-600"> · {ev.location.name}</span>}
            </p>
            <p className="text-sm text-slate-900">{ev.description}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {ev.needs_review && <Badge tone="warning">needs review</Badge>}
              {ev.confidence !== null && <span className="text-xs text-slate-500">confidence {Math.round(ev.confidence * 100)}%</span>}
              {ev.sources.map((s) => (
                <SourceButton key={`${s.chunk_id}-${s.locator.kind}`} target={{ evidenceId: s.evidence_id, locator: s.locator, origin: "Timeline source" }} />
              ))}
            </div>
          </li>
        ))}
      </ol>
      {timeline.hasNextPage && (
        <Button
          variant="secondary"
          disabled={timeline.isFetchingNextPage}
          onClick={() => {
            void timeline.fetchNextPage();
          }}
        >
          Load more events
        </Button>
      )}
    </div>
  );
}
