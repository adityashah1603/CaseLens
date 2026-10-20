import { ShieldCheck, ShieldX } from "lucide-react";
import { useState } from "react";
import { useAdminAudit, useMe, useVerifyAudit, type AuditFilter } from "../../api/queries.ts";
import { Empty, ErrorBox, Loading } from "../../components/Feedback.tsx";
import { Button } from "../../components/ui/button.tsx";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card.tsx";
import { AuditFilters, AuditTable } from "../audit/AuditTable.tsx";

function AdminAudit() {
  const [filter, setFilter] = useState<AuditFilter & { case_id?: string }>({});
  const audit = useAdminAudit(filter);
  const verify = useVerifyAudit();
  const entries = audit.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Verify the whole audit hash chain</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <Button
            disabled={verify.isPending}
            onClick={() => {
              verify.mutate();
            }}
          >
            {verify.isPending ? "Verifying" : "Run verification"}
          </Button>
          {verify.data !== undefined &&
            (verify.data.valid ? (
              <p role="status" className="flex items-center gap-1.5 text-sm text-emerald-800">
                <ShieldCheck className="size-4" aria-hidden /> Chain valid: {verify.data.checked_rows} rows recomputed.
              </p>
            ) : (
              <p role="alert" className="flex items-center gap-1.5 text-sm text-red-800">
                <ShieldX className="size-4" aria-hidden /> Chain broken at row {verify.data.first_invalid_id ?? "?"} (
                {verify.data.checked_rows} rows checked).
              </p>
            ))}
          {verify.isError && <ErrorBox error={verify.error} />}
        </CardContent>
      </Card>
      <AuditFilters onApply={setFilter} extra="case_id" />
      {audit.isPending && <Loading label="Loading audit log" />}
      {audit.isError && <ErrorBox error={audit.error} />}
      {audit.isSuccess && entries.length === 0 && <Empty>No entries match.</Empty>}
      {entries.length > 0 && <AuditTable entries={entries} showCase />}
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

/** Platform admin: cross-case audit log and full chain verification. */
export function AdminPage() {
  const me = useMe();
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4">
      <h1 className="text-lg font-semibold text-slate-900">Administration</h1>
      {me.isPending && <Loading />}
      {me.isError && <ErrorBox error={me.error} />}
      {me.data !== undefined && !me.data.user.is_admin && <Empty>This page is for platform administrators.</Empty>}
      {me.data?.user.is_admin === true && <AdminAudit />}
    </main>
  );
}
