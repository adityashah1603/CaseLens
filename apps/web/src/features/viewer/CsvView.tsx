import { useEffect, useMemo, useRef, useState } from "react";
import type { RecordLocator } from "../../api/types.ts";
import { ErrorBox, Loading } from "../../components/Feedback.tsx";
import { cn } from "../../lib/utils.ts";
import { csvWindow, type CsvWindow } from "./csv.ts";

/** Shows the cited CSV row (by `row_id`) with its neighbours. */
export function CsvView({ blob, locator }: { blob: Blob; locator: RecordLocator }) {
  const [loaded, setLoaded] = useState<{ blob: Blob; text: string | null; error: unknown } | null>(null);
  const citedRef = useRef<HTMLTableRowElement>(null);

  useEffect(() => {
    let cancelled = false;
    blob
      .text()
      .then((t) => {
        if (!cancelled) setLoaded({ blob, text: t, error: null });
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoaded({ blob, text: null, error: e });
      });
    return () => {
      cancelled = true;
    };
  }, [blob]);

  const current = loaded?.blob === blob ? loaded : null;
  const text = current?.text ?? null;
  const error = current?.error ?? null;

  const win: CsvWindow | null = useMemo(() => (text === null ? null : csvWindow(text, locator.row_id)), [text, locator.row_id]);

  useEffect(() => {
    citedRef.current?.scrollIntoView({ block: "nearest" });
  }, [win]);

  if (error !== null) return <ErrorBox title="The table could not be read." error={error} />;
  if (win === null) return <Loading label="Reading table" />;

  return (
    <div className="flex flex-col gap-2" data-testid="csv-view">
      <p className="text-xs text-slate-600">
        Table <code className="font-semibold">{locator.table}</code>, row_id{" "}
        <span className="font-semibold tabular-nums">{locator.row_id}</span>
      </p>
      {!win.found && (
        <p role="alert" className="text-sm text-red-800">
          Row {locator.row_id} was not found in this file.
        </p>
      )}
      <div className="overflow-auto rounded border border-slate-200">
        <table className="w-full border-collapse text-xs">
          <thead className="bg-slate-50 text-left text-slate-600">
            <tr>
              {win.header.map((h) => (
                <th key={h} scope="col" className="whitespace-nowrap border-b border-slate-200 px-2 py-1 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {win.rows.map(({ cells, cited }) => (
              <tr
                key={cells.join("\u0001")}
                ref={cited ? citedRef : undefined}
                aria-current={cited ? "true" : undefined}
                data-testid={cited ? "csv-cited-row" : "csv-row"}
                className={cn("border-b border-slate-100", cited && "bg-yellow-100 font-medium outline-2 -outline-offset-2 outline-orange-500")}
              >
                {cells.map((c, i) => (
                  <td key={`${String(i)}-${c}`} className="whitespace-nowrap px-2 py-1 text-slate-800">
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
