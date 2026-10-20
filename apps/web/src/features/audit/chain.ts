import type { AuditEntry } from "../../api/types.ts";

/**
 * Checks `prev_hash` against the previous row's `row_hash` wherever two loaded rows are adjacent
 * in the chain (consecutive ids). The full recomputation is the server's `/admin/audit/verify`.
 */
export function checkLinks(entries: readonly AuditEntry[]): { checked: number; broken: number[] } {
  const sorted = [...entries].sort((a, b) => a.id - b.id);
  let checked = 0;
  const broken: number[] = [];
  for (let i = 1; i < sorted.length; i++) {
    const prev = sorted[i - 1];
    const cur = sorted[i];
    if (prev === undefined || cur === undefined) continue;
    if (cur.id !== prev.id + 1) continue;
    checked += 1;
    if (cur.prev_hash !== prev.row_hash) broken.push(cur.id);
  }
  return { checked, broken };
}
