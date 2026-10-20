import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { Eye, Radio, Upload } from "lucide-react";
import { useEffect, useRef, useState, type DragEvent, type SubmitEvent } from "react";
import { useApi } from "../../api/ApiContext.ts";
import { ApiError } from "../../api/client.ts";
import { qk, useEvidenceList, useUploadEvidence } from "../../api/queries.ts";
import { followIngestEvents } from "../../api/sse.ts";
import type { Evidence, EvidenceList, EvidenceType, IngestEvent, IngestJob, Locator } from "../../api/types.ts";
import { Empty, ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Badge } from "../../components/ui/badge.tsx";
import { Button } from "../../components/ui/button.tsx";
import { Input, Label, Select, Textarea } from "../../components/ui/input.tsx";
import { errorMessage } from "../../lib/errors.ts";
import { formatBytes, formatTs } from "../../lib/format.ts";
import { cn } from "../../lib/utils.ts";
import { useViewer } from "../viewer/ViewerContext.ts";
import { applyJobUpdate } from "./jobs.ts";

const TYPES: EvidenceType[] = ["pdf", "audio", "video", "csv"];

const STATUS_STYLE: Record<IngestJob["status"], string> = {
  queued: "bg-slate-100 text-slate-600",
  running: "bg-sky-100 text-sky-800 animate-pulse",
  succeeded: "bg-emerald-100 text-emerald-800",
  failed: "bg-amber-100 text-amber-900",
  dead_lettered: "bg-red-100 text-red-800",
};

function Pipeline({ jobs }: { jobs: IngestJob[] }) {
  if (jobs.length === 0) return <span className="text-xs text-slate-500">no jobs</span>;
  const done = jobs.filter((j) => j.status === "succeeded").length;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap gap-1" aria-label={`Pipeline: ${String(done)} of ${String(jobs.length)} stages done`}>
        {jobs.map((j) => (
          <span
            key={j.id}
            className={cn("rounded px-1.5 py-0.5 text-[11px] font-medium", STATUS_STYLE[j.status])}
            title={j.error ?? `${j.stage}: ${j.status} (attempts ${String(j.attempts)})`}
          >
            {j.stage}
            <span className="sr-only">: {j.status}</span>
          </span>
        ))}
      </div>
      <div className="h-1 w-full max-w-60 overflow-hidden rounded bg-slate-200" aria-hidden>
        <div className="h-full bg-emerald-600" style={{ width: `${String((100 * done) / jobs.length)}%` }} />
      </div>
    </div>
  );
}

function openLocator(ev: Evidence): Locator | null {
  if (ev.type === "pdf") return { kind: "pdf", page: 1, bbox: null };
  if (ev.type === "audio") return { kind: "audio", start_ms: 0, end_ms: 0 };
  if (ev.type === "video") return { kind: "video", start_ms: 0 };
  return null;
}

function uploadError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return "A file with the same SHA-256 is already in this case.";
    if (error.status === 413) return "The file is larger than the upload limit.";
    if (error.status === 415) return "Unsupported file type. Upload a PDF, audio, video or CSV file.";
  }
  return errorMessage(error);
}

function UploadForm({ caseId }: { caseId: string }) {
  const upload = useUploadEvidence(caseId);
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState("");
  const [recordedLocal, setRecordedLocal] = useState("");
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f !== undefined) setFile(f);
  };

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (file === null) return;
    const d = recordedLocal === "" ? null : new Date(recordedLocal);
    const recordedAt = d === null || Number.isNaN(d.getTime()) ? "" : d.toISOString();
    upload.mutate(
      { file, description, recordedAt },
      {
        onSuccess: () => {
          setFile(null);
          setDescription("");
          setRecordedLocal("");
          if (inputRef.current !== null) inputRef.current.value = "";
        },
      },
    );
  };

  return (
    <form onSubmit={submit} aria-label="Upload evidence" className="flex flex-col gap-2 rounded-md border border-slate-200 bg-slate-50 p-3">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => {
          setDragging(false);
        }}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-1 rounded border-2 border-dashed p-4 text-sm text-slate-600",
          dragging ? "border-sky-500 bg-sky-50" : "border-slate-300 bg-white",
        )}
      >
        <Upload className="size-5" aria-hidden />
        <span>{file === null ? "Drop a PDF, audio, video or CSV file here, or" : `${file.name} (${formatBytes(file.size)})`}</span>
        <Label htmlFor="ev-file" className="cursor-pointer text-sky-700 underline">
          choose a file
        </Label>
        <input
          ref={inputRef}
          id="ev-file"
          type="file"
          className="sr-only"
          accept=".pdf,.csv,application/pdf,text/csv,audio/*,video/*"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
          }}
        />
      </div>
      <div className="grid gap-2 sm:grid-cols-[1fr_14rem]">
        <div className="flex flex-col gap-1">
          <Label htmlFor="ev-desc">Custody note (optional)</Label>
          <Textarea
            id="ev-desc"
            className="min-h-9"
            maxLength={2000}
            value={description}
            onChange={(e) => {
              setDescription(e.target.value);
            }}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="ev-rec">Recording start (audio/video)</Label>
          <Input
            id="ev-rec"
            type="datetime-local"
            value={recordedLocal}
            onChange={(e) => {
              setRecordedLocal(e.target.value);
            }}
          />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={file === null || upload.isPending}>
          {upload.isPending ? "Uploading" : "Upload"}
        </Button>
        {upload.isSuccess && <span className="text-sm text-emerald-800">Uploaded. Ingest jobs are queued.</span>}
        {upload.isError && (
          <span role="alert" className="text-sm text-red-800">
            {uploadError(upload.error)}
          </span>
        )}
      </div>
    </form>
  );
}

export function EvidencePanel({ caseId, canUpload }: { caseId: string; canUpload: boolean }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { open } = useViewer();
  const [type, setType] = useState<EvidenceType | null>(null);
  const [live, setLive] = useState<"connecting" | "open" | "closed" | "error">("connecting");
  const list = useEvidenceList(caseId, type);

  useEffect(() => {
    const stop = followIngestEvents(api, caseId, {
      onStatus: setLive,
      onEvent: (event: IngestEvent) => {
        if (event.event === "job.updated" && event.job !== null) {
          const job = event.job;
          queryClient.setQueriesData<InfiniteData<EvidenceList>>({ queryKey: qk.evidenceAll(caseId) }, (d) => applyJobUpdate(d, job));
        } else {
          void queryClient.invalidateQueries({ queryKey: qk.evidenceAll(caseId) });
          void queryClient.invalidateQueries({ queryKey: qk.case(caseId) });
        }
      },
    });
    return stop;
  }, [api, caseId, queryClient]);

  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor="ev-type">Modality</Label>
          <Select
            id="ev-type"
            value={type ?? ""}
            onChange={(e) => {
              setType(e.target.value === "" ? null : (e.target.value as EvidenceType));
            }}
          >
            <option value="">All</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
        </div>
        <span className="inline-flex items-center gap-1 text-xs text-slate-500" role="status" aria-live="polite">
          <Radio className={cn("size-3.5", live === "open" ? "text-emerald-600" : "text-slate-400")} aria-hidden />
          {live === "open" ? "Live ingest updates" : live === "error" ? "Live updates unavailable, retrying" : "Connecting to live updates"}
        </span>
      </div>

      {canUpload && <UploadForm caseId={caseId} />}

      {list.isPending && <Loading label="Loading evidence" />}
      {list.isError && <ErrorBox error={list.error} />}
      {list.isSuccess && items.length === 0 && <Empty>No evidence in this case yet.</Empty>}

      <ul className="flex flex-col gap-2" aria-label="Evidence files">
        {items.map((ev) => {
          const loc = openLocator(ev);
          return (
            <li key={ev.id} className="rounded-md border border-slate-200 bg-white p-3" data-testid="evidence-row">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-900">{ev.original_filename}</p>
                  <p className="text-xs text-slate-500">
                    <Badge className="mr-1">{ev.type}</Badge>
                    {formatBytes(ev.size_bytes)} · uploaded {formatTs(ev.created_at)}
                    {ev.recorded_at !== null && ` · recorded ${formatTs(ev.recorded_at)}`} · sha256{" "}
                    <code>{ev.sha256.slice(0, 12)}</code>
                  </p>
                </div>
                {loc !== null && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      open({ evidenceId: ev.id, locator: loc, origin: "Evidence list" });
                    }}
                  >
                    <Eye aria-hidden /> Open
                  </Button>
                )}
              </div>
              <div className="mt-2">
                <Pipeline jobs={ev.ingest_jobs} />
              </div>
              {ev.chain_of_custody.length > 0 && (
                <details className="mt-2 text-xs text-slate-600">
                  <summary className="cursor-pointer">Chain of custody ({ev.chain_of_custody.length})</summary>
                  <ol className="mt-1 flex flex-col gap-0.5 pl-4">
                    {ev.chain_of_custody.map((c, i) => (
                      <li key={`${String(i)}-${c.at}`}>
                        {formatTs(c.at)}: {c.action}
                        {c.note !== null && ` (${c.note})`}
                      </li>
                    ))}
                  </ol>
                </details>
              )}
            </li>
          );
        })}
      </ul>
      {list.hasNextPage && (
        <Button
          variant="secondary"
          disabled={list.isFetchingNextPage}
          onClick={() => {
            void list.fetchNextPage();
          }}
        >
          Load more
        </Button>
      )}
    </div>
  );
}
