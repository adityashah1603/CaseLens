import { useQuery } from "@tanstack/react-query";
import { ShieldX, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "../../api/ApiContext.ts";
import { fetchEvidenceContent, fetchEvidenceMedia, IntegrityError } from "../../api/content.ts";
import type { AudioLocator, PdfLocator, RecordLocator, VideoLocator } from "../../api/types.ts";
import { useEvidenceItem } from "../../api/queries.ts";
import { ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Button } from "../../components/ui/button.tsx";
import { formatBytes, shortId } from "../../lib/format.ts";
import { LOCATOR_KIND_LABEL, locatorLabel } from "../../lib/locator.ts";
import { CsvView } from "./CsvView.tsx";
import { AudioView, VideoView } from "./MediaViews.tsx";
import { PdfView } from "./PdfView.tsx";
import type { ViewerTarget } from "./ViewerContext.ts";

function ContentError({ error }: { error: unknown }) {
  if (error instanceof IntegrityError) {
    return (
      <div role="alert" className="m-2 flex gap-2 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
        <ShieldX className="mt-0.5 size-5 shrink-0" aria-hidden />
        <div>
          <p className="font-semibold">Integrity check failed: this file is not shown.</p>
          <p>{error.message}</p>
        </div>
      </div>
    );
  }
  return <ErrorBox title="The evidence file could not be loaded." error={error} />;
}

const LOADING_LABEL = "Fetching the original (integrity check on the server)";
const retryUnlessIntegrity = (count: number, err: unknown): boolean => !(err instanceof IntegrityError) && count < 1;

/** PDF and CSV: the whole file is fetched as a Blob and parsed in the page. */
function DocumentBody({ caseId, evidenceId, locator }: { caseId: string; evidenceId: string; locator: PdfLocator | RecordLocator }) {
  const api = useApi();
  const content = useQuery({
    queryKey: ["case", caseId, "content", evidenceId],
    queryFn: ({ signal }) => fetchEvidenceContent(api, caseId, evidenceId, signal),
    staleTime: Infinity,
    gcTime: 5 * 60_000,
    retry: retryUnlessIntegrity,
  });
  if (content.isPending) return <Loading label={LOADING_LABEL} />;
  if (content.isError) return <ContentError error={content.error} />;
  return locator.kind === "pdf" ? <PdfView blob={content.data} locator={locator} /> : <CsvView blob={content.data} locator={locator} />;
}

/** An object URL for a Blob, revoked when it changes; null when there is no Blob. */
function useObjectUrl(blob: Blob | null): string | null {
  const url = useMemo(() => (blob === null ? null : URL.createObjectURL(blob)), [blob]);
  useEffect(
    () => () => {
      if (url !== null) URL.revokeObjectURL(url);
    },
    [url],
  );
  return url;
}

/**
 * Audio and video: the element is pointed at the short-lived signed URL so the browser streams it
 * with Range requests. A fresh URL is requested whenever a citation opens (never cached), and once
 * more if the element fails to load it (the URL expired while the viewer was open).
 */
function MediaBody({ caseId, evidenceId, locator }: { caseId: string; evidenceId: string; locator: AudioLocator | VideoLocator }) {
  const api = useApi();
  const media = useQuery({
    queryKey: ["case", caseId, "media", evidenceId, locator],
    queryFn: ({ signal }) => fetchEvidenceMedia(api, caseId, evidenceId, signal),
    staleTime: 0,
    gcTime: 0,
    retry: retryUnlessIntegrity,
  });
  const [refreshed, setRefreshed] = useState(false);
  const [broken, setBroken] = useState(false);
  const blobUrl = useObjectUrl(media.data?.kind === "blob" ? media.data.blob : null);

  if (media.isPending) return <Loading label={LOADING_LABEL} />;
  if (media.isError) return <ContentError error={media.error} />;
  if (broken) return <ErrorBox title="The media file could not be played." />;
  const src = media.data.kind === "signed" ? media.data.url : blobUrl;
  if (src === null) return <Loading label={LOADING_LABEL} />;

  const onSourceError = () => {
    if (media.data.kind === "signed" && !refreshed) {
      setRefreshed(true);
      void media.refetch();
    } else {
      setBroken(true);
    }
  };
  return locator.kind === "audio" ? (
    <AudioView src={src} locator={locator} onSourceError={onSourceError} />
  ) : (
    <VideoView src={src} locator={locator} onSourceError={onSourceError} />
  );
}

function Body({ caseId, target }: { caseId: string; target: ViewerTarget }) {
  const loc = target.locator;
  switch (loc.kind) {
    case "pdf":
    case "record":
      return <DocumentBody caseId={caseId} evidenceId={target.evidenceId} locator={loc} />;
    case "audio":
    case "video":
      return <MediaBody caseId={caseId} evidenceId={target.evidenceId} locator={loc} />;
  }
}

/** Side panel that shows one cited place in one original evidence file. */
export function EvidenceViewer({ caseId, target, onClose }: { caseId: string; target: ViewerTarget; onClose: () => void }) {
  const meta = useEvidenceItem(caseId, target.evidenceId);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, [target]);

  return (
    <section
      aria-label="Evidence viewer"
      className="flex min-h-0 flex-col border-slate-200 bg-white"
      data-testid="evidence-viewer"
      data-locator-kind={target.locator.kind}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <header className="flex items-start justify-between gap-2 border-b border-slate-200 px-4 py-2.5">
        <div className="min-w-0">
          <h2 ref={headingRef} tabIndex={-1} className="truncate text-sm font-semibold text-slate-900 focus-visible:outline-none">
            {meta.data?.original_filename ?? `Evidence ${shortId(target.evidenceId)}`}
          </h2>
          <p className="text-xs text-slate-500" data-testid="viewer-locator">
            {target.origin !== undefined && `${target.origin}: `}
            {LOCATOR_KIND_LABEL[target.locator.kind]}, {locatorLabel(target.locator)}
            {meta.data !== undefined && ` · ${formatBytes(meta.data.size_bytes)} · sha256 ${meta.data.sha256.slice(0, 12)}`}
          </p>
        </div>
        <Button variant="ghost" size="icon" aria-label="Close evidence viewer" onClick={onClose}>
          <X aria-hidden />
        </Button>
      </header>
      {target.quote !== undefined && target.quote !== null && target.quote !== "" && (
        <blockquote className="mx-4 mt-3 border-l-4 border-orange-400 bg-orange-50 px-3 py-1.5 text-sm text-slate-800">
          "{target.quote}"
        </blockquote>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <Body key={target.evidenceId} caseId={caseId} target={target} />
      </div>
    </section>
  );
}
