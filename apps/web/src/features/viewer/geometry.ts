import type { BBox } from "../../api/types.ts";

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The subset of a pdf.js `PageViewport` the highlight math needs. */
export interface ViewportLike {
  viewBox: number[];
  width: number;
  height: number;
  convertToViewportPoint(x: number, y: number): number[];
}

/**
 * Maps a PyMuPDF-style bbox (PDF points, origin at the page's top-left, y down) to CSS pixels on
 * the rendered pdf.js viewport. pdf.js user space has y up from the bottom of the viewBox, so the
 * y values are flipped against the viewBox before pdf.js applies scale and rotation.
 */
export function pdfBoxToViewport(bbox: BBox, viewport: ViewportLike): Rect {
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = bbox;
  const vx = viewport.viewBox[0] ?? 0;
  const vyTop = viewport.viewBox[3] ?? 0;
  const p0 = viewport.convertToViewportPoint(vx + x0, vyTop - y0);
  const p1 = viewport.convertToViewportPoint(vx + x1, vyTop - y1);
  const ax = p0[0] ?? 0;
  const ay = p0[1] ?? 0;
  const bx = p1[0] ?? 0;
  const by = p1[1] ?? 0;
  return { left: Math.min(ax, bx), top: Math.min(ay, by), width: Math.abs(bx - ax), height: Math.abs(by - ay) };
}

/** Scales a bbox in pixels of the original video frame to the rendered element size. */
export function scaleBox(
  bbox: BBox,
  natural: { width: number; height: number },
  rendered: { width: number; height: number },
): Rect | null {
  if (natural.width <= 0 || natural.height <= 0) return null;
  const sx = rendered.width / natural.width;
  const sy = rendered.height / natural.height;
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = bbox;
  return { left: x0 * sx, top: y0 * sy, width: (x1 - x0) * sx, height: (y1 - y0) * sy };
}
