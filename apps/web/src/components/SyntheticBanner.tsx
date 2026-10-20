/** Always-visible demo-mode notice. Rendered once at the app root, outside every route. */
export function SyntheticBanner() {
  return (
    <div
      role="status"
      aria-label="Synthetic data notice"
      className="shrink-0 bg-amber-300 px-4 py-1 text-center text-xs font-semibold tracking-wide text-amber-950"
    >
      SYNTHETIC DATA: every case, person, plate and phone number here is fictional.
    </div>
  );
}
