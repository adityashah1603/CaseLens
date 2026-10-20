import type { ViewportLike } from "./geometry.ts";

export interface PdfViewport extends ViewportLike {
  scale: number;
}

export interface PdfPageLike {
  getViewport(params: { scale: number }): PdfViewport;
  render(params: { canvas: HTMLCanvasElement; viewport: PdfViewport }): { promise: Promise<void>; cancel(): void };
}

export interface PdfDocumentLike {
  numPages: number;
  getPage(n: number): Promise<PdfPageLike>;
  destroy(): Promise<void>;
}

/** Loads a PDF with pdf.js (imported lazily so the main bundle stays small). */
export async function loadPdf(data: ArrayBuffer): Promise<PdfDocumentLike> {
  const [pdfjs, worker] = await Promise.all([import("pdfjs-dist"), import("pdfjs-dist/build/pdf.worker.min.mjs?url")]);
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
  const doc = await pdfjs.getDocument({ data: new Uint8Array(data) }).promise;
  return doc as unknown as PdfDocumentLike;
}
