/**
 * Router state for links back to the case list. Without it, a demo visitor landing on `/` with a
 * single case goes straight into that case; with it, the list is shown as asked.
 */
export const BROWSE_CASES = { browse: true } as const;

export function isBrowsing(state: unknown): boolean {
  return typeof state === "object" && state !== null && (state as { browse?: unknown }).browse === true;
}
