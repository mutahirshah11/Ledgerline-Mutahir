import { ProofPanel } from "@/components/ProofPanel";
import { ErrorState } from "@/components/states";
import { ApiError } from "@/lib/api";
import { serverApi } from "@/lib/server-api";
import type { Outcome } from "@/lib/wording";

// SCR-07 `/runs/[id]/proof` completeness proof (006-web T061; contracts/screens.md SCR-07).
// Server read of EP-13 (the proof) and EP-05 (the run, for its `counts` by outcome, shown beside
// C1 for context); `ProofPanel` mode="full" does the rendering (SCR-07-AC1 to AC4).

export const metadata = { title: "Completeness proof · Ledgerline" };

export default async function ProofPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const client = await serverApi({ next: `/runs/${id}/proof` });
  let data;
  try {
    data = await Promise.all([
      client.get("/runs/{id}/proof", { path: { id } }),
      client.get("/runs/{id}", { path: { id } }),
    ]);
  } catch (err) {
    if (!(err instanceof ApiError)) throw err; // e.g. a redirect from serverApi()
    return (
      <div className="px-4 py-6">
        <ErrorState error={err} />
      </div>
    );
  }
  const [proof, run] = data;
  return (
    <div className="flex flex-col gap-4 p-4">
      <h1 className="text-lg font-semibold">Completeness proof</h1>
      <ProofPanel proof={proof} mode="full" counts={run.counts as Partial<Record<Outcome, number>>} />
    </div>
  );
}
