// 006-web T057 (US6, SCR-02): server read of EP-04 (?limit=20), plus (2026 redesign) a best-effort
// read of the newest run's detail/groups so the dashboard hero and waffle show real data. Each
// extra read is independently optional — either failing (e.g. the newest run hasn't reconciled
// yet, or just briefly erroring) still renders the dashboard, just without that one section; only
// the main runs list is load-bearing for the page itself.

import { ErrorState } from "@/components/states";
import { RunsTable } from "@/components/RunsTable";
import { ApiError } from "@/lib/api";
import { serverApi } from "@/lib/server-api";

export default async function RunsPage() {
  const client = await serverApi({ next: "/runs" });

  let runs;
  try {
    runs = await client.get("/runs", { query: { limit: 20 } });
  } catch (err) {
    if (!(err instanceof ApiError)) throw err; // e.g. a redirect from serverApi()
    return (
      <div className="mx-auto w-full max-w-6xl p-6">
        <div className="flex flex-col items-center gap-3 rounded-3xl border border-dashed border-border bg-card px-6 py-16 text-center">
          <p className="font-heading text-xl font-bold text-foreground">We couldn&apos;t load your runs</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            This is usually a brief connection hiccup. Your data is safe and nothing was changed.
          </p>
          <ErrorState error={err} />
        </div>
      </div>
    );
  }

  const id = runs[0]?.id;
  const [latest, latestGroups] = id
    ? await Promise.all([
        client.get("/runs/{id}", { path: { id } }).catch(() => null),
        client.get("/runs/{id}/groups", { path: { id } }).catch(() => null),
      ])
    : [null, null];

  return (
    <div className="mx-auto w-full max-w-6xl p-6">
      <RunsTable runs={runs} latest={latest} latestGroups={latestGroups} />
    </div>
  );
}
