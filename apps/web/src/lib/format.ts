const ISO_RE = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$/;

/**
 * Formats an ISO-8601 timestamp as written, keeping its own UTC offset, so evidence times read
 * the same as in the source (no silent conversion to the viewer's zone).
 */
export function formatTs(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "";
  const m = ISO_RE.exec(iso);
  if (m === null) return iso;
  const offset = m[3] === undefined ? "" : m[3] === "Z" ? " UTC" : ` ${m[3]}`;
  return `${m[1] ?? ""} ${m[2] ?? ""}${offset}`;
}

/** Milliseconds as m:ss.s (or h:mm:ss.s). */
export function formatMs(ms: number): string {
  const totalTenths = Math.floor(ms / 100);
  const tenths = totalTenths % 10;
  const totalSec = Math.floor(totalTenths / 10);
  const s = totalSec % 60;
  const totalMin = Math.floor(totalSec / 60);
  const m = totalMin % 60;
  const h = Math.floor(totalMin / 60);
  const ss = `${String(s).padStart(2, "0")}.${String(tenths)}`;
  return h > 0 ? `${String(h)}:${String(m).padStart(2, "0")}:${ss}` : `${String(m)}:${ss}`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${String(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** The first 8 characters of a UUID, for compact display. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}
