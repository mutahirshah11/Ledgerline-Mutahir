// 006-web T057 (US6, SCR-02): server read of EP-04 (?limit=20); the client table/actions live in
// RunsTable.tsx (needs useMe() for the "New run" gate, client-only).

import { serverApi } from "@/lib/server-api";
import { RunsTable } from "@/components/RunsTable";

export default async function RunsPage() {
  const client = await serverApi({ next: "/runs" });
  const runs = await client.get("/runs", { query: { limit: 20 } });
  return <RunsTable runs={runs} />;
}
