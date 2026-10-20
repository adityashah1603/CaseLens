import { useState, type SubmitEvent } from "react";
import type { AuditFilter } from "../../api/queries.ts";
import type { AuditEntry } from "../../api/types.ts";
import { Badge } from "../../components/ui/badge.tsx";
import { Button } from "../../components/ui/button.tsx";
import { Input, Label } from "../../components/ui/input.tsx";
import { formatTs, shortId } from "../../lib/format.ts";
import { checkLinks } from "./chain.ts";

function toIso(local: string): string | undefined {
  if (local === "") return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export function AuditFilters({
  onApply,
  extra,
}: {
  onApply: (f: AuditFilter & { case_id?: string }) => void;
  extra?: "case_id";
}) {
  const [action, setAction] = useState("");
  const [userId, setUserId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    const f: AuditFilter & { case_id?: string } = {};
    if (action.trim() !== "") f.action = action.trim();
    if (userId.trim() !== "") f.user_id = userId.trim();
    if (extra === "case_id" && caseId.trim() !== "") f.case_id = caseId.trim();
    const fi = toIso(from);
    const ti = toIso(to);
    if (fi !== undefined) f.from = fi;
    if (ti !== undefined) f.to = ti;
    onApply(f);
  };

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2" aria-label="Audit filters">
      <div className="flex flex-col gap-1">
        <Label htmlFor="au-action">Action</Label>
        <Input
          id="au-action"
          placeholder="e.g. evidence.view"
          maxLength={100}
          value={action}
          onChange={(e) => {
            setAction(e.target.value);
          }}
        />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="au-user">User id</Label>
        <Input
          id="au-user"
          placeholder="UUID"
          value={userId}
          onChange={(e) => {
            setUserId(e.target.value);
          }}
        />
      </div>
      {extra === "case_id" && (
        <div className="flex flex-col gap-1">
          <Label htmlFor="au-case">Case id</Label>
          <Input
            id="au-case"
            placeholder="UUID"
            value={caseId}
            onChange={(e) => {
              setCaseId(e.target.value);
            }}
          />
        </div>
      )}
      <div className="flex flex-col gap-1">
        <Label htmlFor="au-from">From</Label>
        <Input
          id="au-from"
          type="datetime-local"
          value={from}
          onChange={(e) => {
            setFrom(e.target.value);
          }}
        />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="au-to">To</Label>
        <Input
          id="au-to"
          type="datetime-local"
          value={to}
          onChange={(e) => {
            setTo(e.target.value);
          }}
        />
      </div>
      <Button type="submit" variant="secondary">
        Filter
      </Button>
    </form>
  );
}

export function AuditTable({ entries, showCase = false }: { entries: AuditEntry[]; showCase?: boolean }) {
  const links = checkLinks(entries);
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-slate-600" data-testid="chain-check">
        {links.checked === 0
          ? "No adjacent chain rows on this page to cross-check."
          : links.broken.length === 0
            ? `${String(links.checked)} adjacent hash links checked on this page: all consistent.`
            : `${String(links.broken.length)} of ${String(links.checked)} adjacent hash links do not match (rows ${links.broken.join(", ")}).`}
      </p>
      <div className="overflow-auto rounded border border-slate-200">
        <table className="w-full border-collapse text-xs">
          <thead className="bg-slate-50 text-left text-slate-600">
            <tr>
              <th scope="col" className="px-2 py-1.5 font-semibold">#</th>
              <th scope="col" className="px-2 py-1.5 font-semibold">Time</th>
              <th scope="col" className="px-2 py-1.5 font-semibold">Action</th>
              <th scope="col" className="px-2 py-1.5 font-semibold">User</th>
              {showCase && <th scope="col" className="px-2 py-1.5 font-semibold">Case</th>}
              <th scope="col" className="px-2 py-1.5 font-semibold">Payload</th>
              <th scope="col" className="px-2 py-1.5 font-semibold">Row hash</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id} className="border-t border-slate-100 align-top" data-testid="audit-row">
                <td className="px-2 py-1 tabular-nums text-slate-500">{e.id}</td>
                <td className="whitespace-nowrap px-2 py-1 tabular-nums">{formatTs(e.ts)}</td>
                <td className="px-2 py-1">
                  <code className="font-medium">{e.action}</code>
                </td>
                <td className="px-2 py-1">{e.user_id === null ? <Badge>system</Badge> : <code title={e.user_id}>{shortId(e.user_id)}</code>}</td>
                {showCase && <td className="px-2 py-1">{e.case_id === null ? "" : <code title={e.case_id}>{shortId(e.case_id)}</code>}</td>}
                <td className="max-w-md px-2 py-1">
                  <details>
                    <summary className="cursor-pointer truncate text-slate-600">{Object.keys(e.payload).join(", ") || "{}"}</summary>
                    <pre className="mt-1 whitespace-pre-wrap break-all text-[11px] text-slate-700">{JSON.stringify(e.payload, null, 1)}</pre>
                  </details>
                </td>
                <td className="px-2 py-1">
                  <code title={`row ${e.row_hash}\nprev ${e.prev_hash}\npayload ${e.payload_hash}`}>{e.row_hash.slice(0, 10)}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
