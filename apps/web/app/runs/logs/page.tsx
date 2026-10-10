// 2026 redesign: the full, unfiltered run history. The dashboard (/runs) hides FAILED runs to
// stay uncluttered (user request) — nothing is ever deleted (apps/api audit_log is append-only),
// so every run, including failed/old ones, stays reachable here via the sidebar's "Logs" link.

import { ScrollText } from "lucide-react";

import { MonthlyTable } from "@/components/dashboard/MonthlyTable";
import { ErrorState } from "@/components/states";
import { ApiError } from "@/lib/api";
import { serverApi } from "@/lib/server-api";

export default async function LogsPage() {
  const client = await serverApi({ next: "/runs/logs" });

  let runs;
  try {
    runs = await client.get("/runs", { query: { limit: 100 } });
  } catch (err) {
    if (!(err instanceof ApiError)) throw err; // e.g. a redirect from serverApi()
    return (
      <div className="mx-auto w-full max-w-6xl p-6">
        <ErrorState error={err} />
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-6">
      <div className="flex items-start gap-3">
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-primary-soft text-primary">
          <ScrollText className="h-5 w-5" aria-hidden="true" />
        </span>
        <div>
          <h1 className="font-heading text-2xl font-bold tracking-tight text-foreground">Logs</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Every reconciliation run ever attempted, including failed ones. Nothing here is ever deleted.
          </p>
        </div>
      </div>
      {runs.length === 0 ? (
        <p className="text-sm text-muted-foreground">No runs yet.</p>
      ) : (
        <MonthlyTable runs={runs} />
      )}
    </div>
  );
}
