import type { Locator } from "../api/types.ts";
import { formatMs } from "./format.ts";

/** A short, human label for where a citation points ("p. 2", "0:41.2 to 0:52.9", "plate_reads row 118"). */
export function locatorLabel(loc: Locator): string {
  switch (loc.kind) {
    case "pdf":
      return `p. ${String(loc.page)}`;
    case "audio":
      return `${formatMs(loc.start_ms)} to ${formatMs(loc.end_ms)}`;
    case "video":
      return loc.frame_idx === null || loc.frame_idx === undefined
        ? formatMs(loc.start_ms)
        : `${formatMs(loc.start_ms)}, frame ${String(loc.frame_idx)}`;
    case "record":
      return `${loc.table} row ${String(loc.row_id)}`;
  }
}

export const LOCATOR_KIND_LABEL: Record<Locator["kind"], string> = {
  pdf: "PDF",
  audio: "Audio",
  video: "Video",
  record: "Table row",
};
