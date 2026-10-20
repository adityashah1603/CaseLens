import { Loader2, Send, Square } from "lucide-react";
import { useEffect, useRef, useState, type SubmitEvent } from "react";
import { useAsk, useRun, useRuns } from "../../api/queries.ts";
import type { AgentRun, Case } from "../../api/types.ts";
import { ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Badge } from "../../components/ui/badge.tsx";
import { Button } from "../../components/ui/button.tsx";
import { Label, Textarea } from "../../components/ui/input.tsx";
import { formatTs } from "../../lib/format.ts";
import { AnswerView } from "./AnswerView.tsx";
import { ToolTrace } from "./ToolTrace.tsx";

/** Seconds since mount; mounted only while a question is in flight. */
function ElapsedSeconds() {
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(started);
  useEffect(() => {
    const t = setInterval(() => {
      setNow(Date.now());
    }, 250);
    return () => {
      clearInterval(t);
    };
  }, []);
  return (
    <span className="tabular-nums" data-testid="elapsed">
      {Math.max(0, Math.floor((now - started) / 1000))} s
    </span>
  );
}

function RunResult({ run }: { run: AgentRun }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm font-medium text-slate-900">
        <span className="text-slate-500">Q: </span>
        {run.question}
      </p>
      {run.answer === null ? (
        <ErrorBox title="The agent run did not produce an answer.">
          <p>Run status: {run.status}. See the trace for details.</p>
        </ErrorBox>
      ) : (
        <AnswerView answer={run.answer} />
      )}
      <ToolTrace run={run} />
    </div>
  );
}

/** The core screen: ask a question, read the cited answer, open any source. */
export function AskPanel({ caseData }: { caseData: Case }) {
  const caseId = caseData.id;
  const [question, setQuestion] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const ask = useAsk(caseId);
  const runs = useRuns(caseId);
  const selected = useRun(caseId, selectedRunId);

  useEffect(() => () => abortRef.current?.abort(), []);

  const submit = (q: string) => {
    const text = q.trim();
    if (text === "" || ask.isPending) return;
    setQuestion(text);
    setCancelled(false);
    setSelectedRunId(null);
    const controller = new AbortController();
    abortRef.current = controller;
    ask.mutate(
      { question: text, signal: controller.signal },
      {
        onSuccess: (run) => {
          setSelectedRunId(run.id);
        },
      },
    );
  };

  const onSubmit = (e: SubmitEvent) => {
    e.preventDefault();
    submit(question);
  };

  const stopWaiting = () => {
    abortRef.current?.abort();
    setCancelled(true);
    ask.reset();
  };

  const shownRun: AgentRun | undefined = selectedRunId === null ? undefined : (selected.data ?? undefined);

  return (
    <div className="flex flex-col gap-4 p-4">
      <form onSubmit={onSubmit} className="flex flex-col gap-2" aria-label="Ask the investigator agent">
        <Label htmlFor="question">Question</Label>
        <Textarea
          id="question"
          value={question}
          maxLength={2000}
          placeholder="Ask about this case, e.g. Who was in the gray sedan on March 14?"
          onChange={(e) => {
            setQuestion(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit(question);
            }
          }}
          disabled={ask.isPending}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={ask.isPending || question.trim() === ""}>
            <Send aria-hidden /> Ask
          </Button>
          <span className="text-xs text-slate-500">Ctrl+Enter to send. Answers cite their sources or refuse.</span>
        </div>
        {caseData.suggested_questions.length > 0 && (
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-500">Suggested questions</span>
            <div className="flex flex-wrap gap-1.5">
              {caseData.suggested_questions.map((q) => (
                <Button
                  key={q}
                  variant="secondary"
                  size="sm"
                  disabled={ask.isPending}
                  onClick={() => {
                    submit(q);
                  }}
                  className="h-auto whitespace-normal py-1 text-left"
                >
                  {q}
                </Button>
              ))}
            </div>
          </div>
        )}
      </form>

      {ask.isPending && (
        <div role="status" aria-live="polite" className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
          <div className="flex items-center justify-between gap-2">
            <p className="flex items-center gap-2">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              The agent is searching the evidence and checking every citation.
              <ElapsedSeconds />
            </p>
            <Button variant="secondary" size="sm" onClick={stopWaiting}>
              <Square aria-hidden /> Stop waiting
            </Button>
          </div>
          <div className="mt-2 h-1 overflow-hidden rounded bg-sky-100">
            <div className="h-full w-1/3 animate-pulse rounded bg-sky-500" />
          </div>
          <p className="mt-1 text-xs text-sky-800">Usually 20 to 60 seconds.</p>
        </div>
      )}

      {cancelled && !ask.isPending && (
        <p className="text-sm text-slate-600" role="status">
          Stopped waiting. The run may still finish on the server; it will appear under Recent questions.
        </p>
      )}

      {ask.isError && !cancelled && <ErrorBox title="The question could not be answered." error={ask.error} />}

      {selectedRunId !== null && selected.isPending && <Loading label="Loading run" />}
      {selected.isError && <ErrorBox error={selected.error} />}
      {shownRun !== undefined && <RunResult run={shownRun} />}

      <section aria-label="Recent questions" className="border-t border-slate-200 pt-3">
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Recent questions</h3>
        {runs.isPending && <Loading />}
        {runs.data?.items.length === 0 && <p className="text-sm text-slate-500">None yet.</p>}
        <ul className="flex flex-col">
          {runs.data?.items.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                onClick={() => {
                  setCancelled(false);
                  setSelectedRunId(r.id);
                }}
                aria-current={r.id === selectedRunId ? "true" : undefined}
                className="flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 aria-[current=true]:bg-slate-100"
              >
                <span className="truncate text-slate-800">{r.question}</span>
                <span className="flex shrink-0 items-center gap-2 text-xs text-slate-500">
                  {r.answer_status === "insufficient_evidence" && <Badge tone="warning">insufficient</Badge>}
                  {r.status === "failed" && <Badge tone="danger">failed</Badge>}
                  {formatTs(r.created_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
