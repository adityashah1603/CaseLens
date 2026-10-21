import { Info } from "lucide-react";

/** What the public demo is, in a few lines (sign-in screen and the demo case header). */
export function AboutDemoText() {
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
      <li>
        CaseLens is an evidence-search copilot: ask a question about a mixed case file (PDFs, scanned pages, audio,
        video, tables) and get a timeline of answers.
      </li>
      <li>
        Every claim is cited to its exact place in the evidence (a page region, an audio span, a video frame or a
        table row), and a validator withholds anything it cannot cite. Click a citation to see the original.
      </li>
      <li>
        The demo case, &ldquo;The Riverside Lot Homicide&rdquo;, is entirely SYNTHETIC: invented people, places and
        events.
      </li>
      <li>The demo runs on a free-tier LLM, so an answer takes about 10 to 60 seconds.</li>
    </ul>
  );
}

/** A collapsible "About this demo" panel. */
export function AboutDemo() {
  return (
    <details className="group text-sm" data-testid="about-demo">
      <summary className="inline-flex cursor-pointer items-center gap-1 text-xs font-medium text-sky-800 hover:text-sky-950">
        <Info className="size-3.5" aria-hidden /> About this demo
      </summary>
      <div className="mt-2 max-w-2xl rounded-md border border-slate-200 bg-slate-50 p-3">
        <AboutDemoText />
      </div>
    </details>
  );
}
