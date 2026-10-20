import { FileText, Film, Mic, Table } from "lucide-react";
import type { Locator } from "../../api/types.ts";
import { LOCATOR_KIND_LABEL, locatorLabel } from "../../lib/locator.ts";
import { cn } from "../../lib/utils.ts";
import { useViewer, type ViewerTarget } from "./ViewerContext.ts";

const ICONS = { pdf: FileText, audio: Mic, video: Film, record: Table } as const;

export function LocatorIcon({ kind, className }: { kind: Locator["kind"]; className?: string }) {
  const Icon = ICONS[kind];
  return <Icon className={cn("size-3.5", className)} aria-hidden />;
}

/** A link-styled button that opens a source in the evidence viewer. */
export function SourceButton({ target, className }: { target: ViewerTarget; className?: string }) {
  const { open } = useViewer();
  const where = locatorLabel(target.locator);
  return (
    <button
      type="button"
      onClick={() => {
        open(target);
      }}
      className={cn(
        "inline-flex items-center gap-1 rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-xs text-sky-800 hover:border-sky-300 hover:bg-sky-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600",
        className,
      )}
      aria-label={`Open ${LOCATOR_KIND_LABEL[target.locator.kind]} source, ${where}, in the evidence viewer`}
    >
      <LocatorIcon kind={target.locator.kind} />
      {where}
    </button>
  );
}
