import { LOCATOR_KIND_LABEL, locatorLabel } from "../../lib/locator.ts";
import { useViewer } from "../viewer/ViewerContext.ts";
import type { NumberedCitation } from "./resolve.ts";

/** Numbered citation chips; each opens the evidence viewer at the cited locator. */
export function CitationChips({ cited }: { cited: NumberedCitation[] }) {
  const { open } = useViewer();
  return (
    <span className="inline-flex flex-wrap gap-1 align-middle">
      {cited.map(({ citation, n }) => (
        <button
          key={citation.id}
          type="button"
          data-citation-id={citation.id}
          onClick={() => {
            open({ evidenceId: citation.evidence_id, locator: citation.locator, quote: citation.quote ?? null, origin: `Citation ${String(n)}` });
          }}
          title={`${LOCATOR_KIND_LABEL[citation.locator.kind]}, ${locatorLabel(citation.locator)}`}
          aria-label={`Citation ${String(n)}: ${LOCATOR_KIND_LABEL[citation.locator.kind]}, ${locatorLabel(citation.locator)}`}
          className="inline-flex h-5 min-w-5 items-center justify-center rounded bg-sky-100 px-1 text-[11px] font-semibold text-sky-900 ring-1 ring-sky-300 hover:bg-sky-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-700"
        >
          {n}
        </button>
      ))}
    </span>
  );
}
