import { createContext, useContext } from "react";
import type { Locator } from "../../api/types.ts";

/** One place in one evidence file: what a citation, timeline source or mention points at. */
export interface ViewerTarget {
  evidenceId: string;
  locator: Locator;
  quote?: string | null;
  /** Where the click came from, for the viewer heading (e.g. "Citation 2"). */
  origin?: string;
}

export interface ViewerState {
  target: ViewerTarget | null;
  open: (target: ViewerTarget) => void;
  close: () => void;
}

export const ViewerContext = createContext<ViewerState | null>(null);

export function useViewer(): ViewerState {
  const state = useContext(ViewerContext);
  if (state === null) throw new Error("useViewer must be used inside a ViewerContext provider");
  return state;
}
