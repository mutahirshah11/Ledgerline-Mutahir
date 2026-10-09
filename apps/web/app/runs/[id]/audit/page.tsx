import { ErrorState } from "@/components/states";
import { ApiError } from "@/lib/api";
import { serverApi } from "@/lib/server-api";

import { AuditView } from "./AuditView";

// SCR-08 `/runs/[id]/audit` (006-web T062; contracts/screens.md SCR-08). Server read of EP-20
// (audit log) and EP-19 (run events); `AuditView` (client) renders the table, timeline and the
// actor filter (SCR-08-AC1 to AC3).

export const metadata = { title: "Audit · Ledgerline" };

export default async function AuditPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const client = await serverApi({ next: `/runs/${id}/audit` });
  let data;
  try {
    data = await Promise.all([
      client.get("/runs/{id}/audit", { path: { id } }),
      client.get("/runs/{id}/events", { path: { id } }),
    ]);
  } catch (err) {
    if (!(err instanceof ApiError)) throw err; // e.g. a redirect from serverApi()
    return (
      <div className="px-4 py-6">
        <ErrorState error={err} />
      </div>
    );
  }
  const [entries, events] = data;
  return <AuditView entries={entries} events={events} />;
}
