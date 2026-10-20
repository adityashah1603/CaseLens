import { useMemo } from "react";
import type { EntityType, Graph } from "../../api/types.ts";
import { TYPE_COLOR } from "./colors.ts";
import { forceLayout } from "./layout.ts";

const W = 760;
const H = 520;

/** Entity graph: nodes by type, `same_as` merges solid, co-mentions dashed. Click a node for detail. */
export function GraphView({
  graph,
  selectedId,
  onSelect,
}: {
  graph: Graph;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const pos = useMemo(
    () => forceLayout(graph.nodes.map((n) => n.id), graph.edges, W, H),
    [graph],
  );
  const maxMentions = Math.max(1, ...graph.nodes.map((n) => n.mention_count));

  return (
    <figure className="flex flex-col gap-2">
      <svg viewBox={`0 0 ${String(W)} ${String(H)}`} className="w-full rounded border border-slate-200 bg-white" role="group" aria-label="Entity graph">
        {graph.edges.map((e) => {
          const a = pos.get(e.source);
          const b = pos.get(e.target);
          if (a === undefined || b === undefined) return null;
          return (
            <line
              key={e.id}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={e.kind === "same_as" ? "#0f172a" : "#94a3b8"}
              strokeWidth={e.kind === "same_as" ? 2 : Math.min(4, 0.75 + e.weight * 0.5)}
              strokeDasharray={e.kind === "co_mention" ? "4 3" : undefined}
              opacity={0.8}
            />
          );
        })}
        {graph.nodes.map((n) => {
          const p = pos.get(n.id);
          if (p === undefined) return null;
          const r = 6 + 10 * Math.sqrt(n.mention_count / maxMentions);
          const selected = n.id === selectedId;
          return (
            <g
              key={n.id}
              transform={`translate(${String(p.x)} ${String(p.y)})`}
              role="button"
              tabIndex={0}
              aria-label={`${n.label} (${n.type}), ${String(n.mention_count)} mentions`}
              aria-pressed={selected}
              className="cursor-pointer focus:outline-none [&:focus-visible>circle]:stroke-sky-500"
              onClick={() => {
                onSelect(n.id);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(n.id);
                }
              }}
            >
              <circle r={r} fill={TYPE_COLOR[n.type]} stroke={selected ? "#0f172a" : "#fff"} strokeWidth={selected ? 3 : 1.5} />
              <text y={r + 11} textAnchor="middle" className="fill-slate-700 text-[10px]">
                {n.label.length > 22 ? `${n.label.slice(0, 21)}…` : n.label}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption className="flex flex-wrap gap-3 text-xs text-slate-600">
        {(Object.keys(TYPE_COLOR) as EntityType[]).map((t) => (
          <span key={t} className="inline-flex items-center gap-1">
            <span className="size-2.5 rounded-full" style={{ background: TYPE_COLOR[t] }} aria-hidden />
            {t}
          </span>
        ))}
        <span>solid line: merged (same_as) · dashed: co-mentioned</span>
      </figcaption>
    </figure>
  );
}
