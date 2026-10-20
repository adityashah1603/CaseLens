import type { AgentRun } from "../../api/types.ts";
import { Badge } from "../../components/ui/badge.tsx";

function fmtCost(v: number | null): string {
  return v === null ? "n/a" : `$${v.toFixed(4)}`;
}

/** The agent trace drawer: plan, each tool call (input, summary, SQL, latency), totals. */
export function ToolTrace({ run }: { run: AgentRun }) {
  return (
    <details className="rounded-md border border-slate-200 bg-slate-50 text-sm" aria-label="Agent trace">
      <summary className="cursor-pointer select-none px-3 py-2 font-medium text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600">
        Agent trace: {run.tool_calls.length} tool call{run.tool_calls.length === 1 ? "" : "s"}
        {run.latency_ms !== null && `, ${(run.latency_ms / 1000).toFixed(1)} s`}
      </summary>
      <div className="flex flex-col gap-3 border-t border-slate-200 px-3 py-2">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-slate-600 sm:grid-cols-4">
          <div>
            <dt className="text-slate-400">Model</dt>
            <dd>{run.model ?? "n/a"}</dd>
          </div>
          <div>
            <dt className="text-slate-400">Tokens in / out</dt>
            <dd className="tabular-nums">
              {run.input_tokens ?? "n/a"} / {run.output_tokens ?? "n/a"}
            </dd>
          </div>
          <div>
            <dt className="text-slate-400">Cost</dt>
            <dd className="tabular-nums">{fmtCost(run.cost_usd)}</dd>
          </div>
          <div>
            <dt className="text-slate-400">Run status</dt>
            <dd>{run.status}</dd>
          </div>
        </dl>
        {run.plan !== null && run.plan.length > 0 && (
          <div>
            <h4 className="text-xs font-semibold text-slate-500">Plan</h4>
            <ol className="list-decimal pl-5 text-xs text-slate-700">
              {run.plan.map((step, i) => (
                <li key={`${String(i)}-${step}`}>{step}</li>
              ))}
            </ol>
          </div>
        )}
        <ol className="flex flex-col gap-2">
          {run.tool_calls.map((call, i) => (
            <li key={`${String(i)}-${call.name}`} className="rounded border border-slate-200 bg-white p-2" data-testid="tool-call">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-slate-400">#{i + 1}</span>
                <code className="text-xs font-semibold text-slate-900">{call.name}</code>
                <span className="text-xs tabular-nums text-slate-500">{call.latency_ms} ms</span>
                {call.error !== null && <Badge tone="danger">error</Badge>}
              </div>
              <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-1.5 text-[11px] text-slate-700">
                {JSON.stringify(call.input, null, 1)}
              </pre>
              <p className="mt-1 text-xs text-slate-700">{call.output_summary}</p>
              {call.sql !== null && (
                <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-slate-900 p-1.5 text-[11px] text-slate-100">
                  {call.sql}
                </pre>
              )}
              {call.error !== null && <p className="mt-1 text-xs text-red-700">{call.error}</p>}
            </li>
          ))}
        </ol>
      </div>
    </details>
  );
}
