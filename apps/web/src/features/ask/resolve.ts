import type { AnswerContract, Citation, Claim, Conflict, ConflictSide } from "../../api/types.ts";

export interface NumberedCitation {
  citation: Citation;
  /** 1-based display number, by position in `answer.citations`. */
  n: number;
}

export interface ResolvedRefs {
  cited: NumberedCitation[];
  /** Citation ids that do not resolve to an entry in `citations`. */
  missing: string[];
  /** True only when there is at least one citation and every id resolves. */
  ok: boolean;
}

export interface ResolvedAnswer {
  byId: Map<string, NumberedCitation>;
  claims: { claim: Claim; refs: ResolvedRefs }[];
  conflicts: { conflict: Conflict; a: ResolvedRefs; b: ResolvedRefs }[];
  /** Answered status but nothing renderable: shown as an integrity failure, never as prose alone. */
  unsupported: boolean;
}

function resolveRefs(ids: readonly string[], byId: Map<string, NumberedCitation>): ResolvedRefs {
  const cited: NumberedCitation[] = [];
  const missing: string[] = [];
  for (const id of ids) {
    const hit = byId.get(id);
    if (hit === undefined) missing.push(id);
    else if (!cited.includes(hit)) cited.push(hit);
  }
  return { cited, missing, ok: cited.length > 0 && missing.length === 0 };
}

/**
 * Resolves every claim's and conflict side's citation ids against `citations[]`. The UI renders a
 * claim only when `refs.ok`, so an uncited claim can never be shown as if it were supported.
 */
export function resolveAnswer(answer: AnswerContract): ResolvedAnswer {
  const byId = new Map<string, NumberedCitation>();
  answer.citations.forEach((citation, i) => {
    if (!byId.has(citation.id)) byId.set(citation.id, { citation, n: i + 1 });
  });
  const claims = answer.claims.map((claim) => ({ claim, refs: resolveRefs(claim.citation_ids, byId) }));
  const side = (s: ConflictSide) => resolveRefs(s.citation_ids, byId);
  const conflicts = answer.conflicts.map((conflict) => ({ conflict, a: side(conflict.claim_a), b: side(conflict.claim_b) }));
  const unsupported = answer.status === "answered" && !claims.some((c) => c.refs.ok);
  return { byId, claims, conflicts, unsupported };
}
