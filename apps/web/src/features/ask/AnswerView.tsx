import { AlertTriangle, CircleHelp, Scale, ShieldAlert } from "lucide-react";
import type { AnswerContract } from "../../api/types.ts";
import { Badge } from "../../components/ui/badge.tsx";
import { formatTs } from "../../lib/format.ts";
import { LOCATOR_KIND_LABEL, locatorLabel } from "../../lib/locator.ts";
import { LocatorIcon } from "../viewer/SourceButton.tsx";
import { useViewer } from "../viewer/ViewerContext.ts";
import { CitationChips } from "./CitationChips.tsx";
import { resolveAnswer, type ResolvedRefs } from "./resolve.ts";

function Withheld({ refs }: { refs: ResolvedRefs }) {
  return (
    <p role="alert" className="flex items-center gap-1.5 text-sm text-red-800">
      <ShieldAlert className="size-4" aria-hidden />
      Statement withheld: its citations ({refs.missing.length > 0 ? refs.missing.join(", ") : "none"}) do not resolve to
      any source.
    </p>
  );
}

function Confidence({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone = value >= 0.75 ? "bg-emerald-600" : value >= 0.5 ? "bg-amber-500" : "bg-red-600";
  return (
    <div className="flex items-center gap-2 text-xs text-slate-600">
      <span>Confidence</span>
      <div
        className="h-1.5 w-24 overflow-hidden rounded bg-slate-200"
        role="meter"
        aria-label="Answer confidence"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <div className={`h-full ${tone}`} style={{ width: `${String(pct)}%` }} />
      </div>
      <span className="tabular-nums">{pct}%</span>
    </div>
  );
}

function timeRange(start: string | null | undefined, end: string | null | undefined): string | null {
  if (start === null || start === undefined) return null;
  return end === null || end === undefined ? formatTs(start) : `${formatTs(start)} to ${formatTs(end)}`;
}

/** Renders an `AnswerContract`: every claim with its numbered citations, conflicts side by side. */
export function AnswerView({ answer }: { answer: AnswerContract }) {
  const resolved = resolveAnswer(answer);
  const { open } = useViewer();
  const insufficient = answer.status === "insufficient_evidence";

  return (
    <article aria-label="Answer" className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        {insufficient ? (
          <Badge tone="warning">
            <CircleHelp className="size-3.5" aria-hidden /> Insufficient evidence
          </Badge>
        ) : (
          <Badge tone="success">Answered with citations</Badge>
        )}
        <Confidence value={answer.confidence} />
      </header>

      {insufficient && (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950" data-testid="insufficient">
          <p className="font-semibold">The case file does not support an answer to this question.</p>
          <p className="mt-1">{answer.answer}</p>
        </div>
      )}

      {!insufficient && resolved.unsupported && (
        <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          This answer has no claim with a resolvable citation, so it is not shown.
        </div>
      )}

      {!insufficient && !resolved.unsupported && (
        <section aria-label="Summary">
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Summary</h3>
          <p className="text-sm leading-relaxed text-slate-800">{answer.answer}</p>
          <p className="mt-1 text-xs text-slate-500">Each statement below links to its source.</p>
        </section>
      )}

      {resolved.conflicts.length > 0 && (
        <section aria-label="Conflicts" className="rounded-md border border-orange-300 bg-orange-50 p-3">
          <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-orange-950">
            <Scale className="size-4" aria-hidden /> Sources disagree
          </h3>
          <ul className="flex flex-col gap-3">
            {resolved.conflicts.map(({ conflict, a, b }) => (
              <li key={conflict.id} data-testid="conflict">
                <p className="mb-2 text-sm text-orange-950">{conflict.description}</p>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {[
                    { label: "Account A", side: conflict.claim_a, refs: a },
                    { label: "Account B", side: conflict.claim_b, refs: b },
                  ].map(({ label, side, refs }) => (
                    <div key={label} className="rounded border border-orange-200 bg-white p-2" data-testid="conflict-side">
                      <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</p>
                      {refs.ok ? (
                        <p className="text-sm text-slate-800">
                          {side.text} <CitationChips cited={refs.cited} />
                        </p>
                      ) : (
                        <Withheld refs={refs} />
                      )}
                    </div>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {resolved.claims.length > 0 && (
        <section aria-label="Claims">
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Claims</h3>
          <ol className="flex flex-col gap-2">
            {resolved.claims.map(({ claim, refs }) => {
              const when = timeRange(claim.t_start, claim.t_end);
              return (
                <li key={claim.id} data-testid="claim" className="rounded-md border border-slate-200 bg-white p-2.5">
                  {refs.ok ? (
                    <>
                      <p className="text-sm text-slate-900">
                        {claim.text} <CitationChips cited={refs.cited} />
                      </p>
                      {when !== null && <p className="mt-1 text-xs text-slate-500">{when}</p>}
                    </>
                  ) : (
                    <Withheld refs={refs} />
                  )}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {answer.open_questions.length > 0 && (
        <section aria-label="Open questions">
          <h3 className="mb-1 flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
            <AlertTriangle className="size-3.5" aria-hidden /> Open questions
          </h3>
          <ul className="list-disc pl-5 text-sm text-slate-700">
            {answer.open_questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </section>
      )}

      {answer.citations.length > 0 && (
        <section aria-label="Sources">
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Sources</h3>
          <ol className="flex flex-col gap-1">
            {answer.citations.map((c, i) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => {
                    open({ evidenceId: c.evidence_id, locator: c.locator, quote: c.quote ?? null, origin: `Citation ${String(i + 1)}` });
                  }}
                  className="flex w-full items-start gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
                >
                  <span className="mt-px inline-flex h-4 min-w-4 items-center justify-center rounded bg-sky-100 px-1 text-[10px] font-semibold text-sky-900">
                    {i + 1}
                  </span>
                  <LocatorIcon kind={c.locator.kind} className="mt-0.5 text-slate-500" />
                  <span className="text-slate-700">
                    <span className="font-medium">
                      {LOCATOR_KIND_LABEL[c.locator.kind]}, {locatorLabel(c.locator)}
                    </span>
                    {c.quote !== null && c.quote !== undefined && c.quote !== "" && (
                      <span className="text-slate-500"> "{c.quote}"</span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </section>
      )}
    </article>
  );
}
