/** Minimal RFC 4180 CSV parser (quoted fields, escaped quotes, CRLF or LF). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let i = 0;
  const src = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  while (i < src.length) {
    const ch = src.charAt(i);
    if (quoted) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        quoted = false;
      } else {
        field += ch;
      }
      i += 1;
      continue;
    }
    if (ch === '"') quoted = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      if (ch === "\r" && src[i + 1] === "\n") i += 1;
    } else field += ch;
    i += 1;
  }
  if (field !== "" || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

export interface CsvWindow {
  header: string[];
  rows: { cells: string[]; cited: boolean }[];
  found: boolean;
}

/**
 * The cited row (matched on the `row_id` column, which ingest requires to be first) with
 * `context` neighbours on each side. Lines starting with `#` (the SYNTHETIC header) are skipped.
 */
export function csvWindow(text: string, rowId: number, context = 3): CsvWindow {
  const all = parseCsv(text).filter((r) => !(r.length > 0 && (r[0] ?? "").startsWith("#")) && r.some((c) => c !== ""));
  const header = all[0] ?? [];
  const body = all.slice(1);
  const col = Math.max(0, header.indexOf("row_id"));
  const idx = body.findIndex((r) => Number(r[col]) === rowId);
  if (idx === -1) return { header, rows: body.slice(0, context * 2 + 1).map((cells) => ({ cells, cited: false })), found: false };
  const start = Math.max(0, idx - context);
  return {
    header,
    rows: body.slice(start, idx + context + 1).map((cells, i) => ({ cells, cited: start + i === idx })),
    found: true,
  };
}
