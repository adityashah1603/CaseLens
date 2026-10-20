import { ChevronLeft, ChevronRight, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { PdfLocator } from "../../api/types.ts";
import { ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Button } from "../../components/ui/button.tsx";
import { pdfBoxToViewport, type Rect } from "./geometry.ts";
import { loadPdf, type PdfDocumentLike } from "./pdfjs.ts";

/** Renders the cited PDF page with pdf.js and outlines the cited bbox. */
export function PdfView({ blob, locator }: { blob: Blob; locator: PdfLocator }) {
  const [loaded, setLoaded] = useState<{ blob: Blob; doc: PdfDocumentLike | null; error: unknown } | null>(null);
  const [page, setPage] = useState(locator.page);
  const [pageFor, setPageFor] = useState(locator);
  const [scale, setScale] = useState(1.25);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [highlight, setHighlight] = useState<Rect | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);

  if (pageFor !== locator) {
    // A new citation in the same file: jump to its page (React "adjust state on prop change").
    setPageFor(locator);
    setPage(locator.page);
  }

  useEffect(() => {
    let cancelled = false;
    let opened: PdfDocumentLike | null = null;
    blob
      .arrayBuffer()
      .then((buf) => loadPdf(buf))
      .then((d) => {
        opened = d;
        if (cancelled) void d.destroy();
        else setLoaded({ blob, doc: d, error: null });
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoaded({ blob, doc: null, error: e });
      });
    return () => {
      cancelled = true;
      if (opened !== null) void opened.destroy();
    };
  }, [blob]);

  const current = loaded?.blob === blob ? loaded : null;
  const doc = current?.doc ?? null;
  const loadError = current?.error ?? null;

  useEffect(() => {
    if (doc === null || canvasRef.current === null) return;
    const canvas = canvasRef.current;
    let cancelled = false;
    let task: { cancel(): void } | null = null;
    void doc.getPage(page).then((p) => {
      if (cancelled) return;
      const viewport = p.getViewport({ scale });
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      setSize({ width: viewport.width, height: viewport.height });
      setHighlight(
        page === locator.page && locator.bbox !== null && locator.bbox !== undefined
          ? pdfBoxToViewport(locator.bbox, viewport)
          : null,
      );
      const rt = p.render({ canvas, viewport });
      task = rt;
      rt.promise.catch(() => {
        // A cancelled render (page or zoom change) is expected.
      });
    });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, page, scale, locator]);

  useEffect(() => {
    highlightRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlight]);

  if (loadError !== null) return <ErrorBox title="The PDF could not be rendered." error={loadError} />;

  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1 text-xs text-slate-600">
        <Button
          variant="secondary"
          size="icon"
          aria-label="Previous page"
          disabled={page <= 1}
          onClick={() => {
            setPage((p) => p - 1);
          }}
        >
          <ChevronLeft aria-hidden />
        </Button>
        <span className="tabular-nums" data-testid="pdf-page">
          Page {page}
          {doc !== null && ` of ${String(doc.numPages)}`}
        </span>
        <Button
          variant="secondary"
          size="icon"
          aria-label="Next page"
          disabled={doc === null || page >= doc.numPages}
          onClick={() => {
            setPage((p) => p + 1);
          }}
        >
          <ChevronRight aria-hidden />
        </Button>
        <Button
          variant="secondary"
          size="icon"
          aria-label="Zoom out"
          onClick={() => {
            setScale((s) => Math.max(0.5, s - 0.25));
          }}
        >
          <ZoomOut aria-hidden />
        </Button>
        <Button
          variant="secondary"
          size="icon"
          aria-label="Zoom in"
          onClick={() => {
            setScale((s) => Math.min(3, s + 0.25));
          }}
        >
          <ZoomIn aria-hidden />
        </Button>
        {page !== locator.page && (
          <Button
            variant="link"
            size="sm"
            onClick={() => {
              setPage(locator.page);
            }}
          >
            Back to cited page {locator.page}
          </Button>
        )}
        {locator.ocr_confidence !== null && locator.ocr_confidence !== undefined && (
          <span className="ml-auto">OCR confidence {Math.round(locator.ocr_confidence * 100)}%</span>
        )}
      </div>
      {doc === null && <Loading label="Rendering PDF" />}
      <div className="overflow-auto rounded border border-slate-200 bg-slate-100 p-2">
        <div className="relative mx-auto w-fit bg-white shadow-sm" style={size === null ? undefined : { width: size.width, height: size.height }}>
          <canvas ref={canvasRef} aria-label={`PDF page ${String(page)}`} role="img" />
          {highlight !== null && (
            <div
              ref={highlightRef}
              data-testid="pdf-highlight"
              aria-label="Cited region"
              className="pointer-events-none absolute rounded-sm bg-yellow-300/30 ring-2 ring-orange-500"
              style={{ left: highlight.left, top: highlight.top, width: highlight.width, height: highlight.height }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
