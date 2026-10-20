import { useState } from "react";
import { useAudit, type AuditFilter } from "../../api/queries.ts";
import { Empty, ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Button } from "../../components/ui/button.tsx";
import { AuditFilters, AuditTable } from "./AuditTable.tsx";

/** Case audit log (lead only). Reading it is itself audited server-side (`audit.view`). */
export function AuditPanel({ caseId, isLead }: { caseId: string; isLead: boolean }) {
  const [filter, setFilter] = useState<AuditFilter>({});
  const audit = useAudit(caseId, filter, isLead);
  if (!isLead) return <Empty>The audit log is visible to the case lead only.</Empty>;
  const entries = audit.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="flex flex-col gap-3 p-4">
      <p className="text-xs text-slate-500">Append-only and hash-chained. Viewing this log is recorded as an audit entry too.</p>
      <AuditFilters onApply={setFilter} />
      {audit.isPending && <Loading label="Loading audit log" />}
      {audit.isError && <ErrorBox error={audit.error} />}
      {audit.isSuccess && entries.length === 0 && <Empty>No entries match.</Empty>}
      {entries.length > 0 && <AuditTable entries={entries} />}
      {audit.hasNextPage && (
        <Button
          variant="secondary"
          disabled={audit.isFetchingNextPage}
          onClick={() => {
            void audit.fetchNextPage();
          }}
        >
          Load more
        </Button>
      )}
    </div>
  );
}
