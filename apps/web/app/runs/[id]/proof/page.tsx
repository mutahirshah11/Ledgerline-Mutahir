import { ShieldCheck } from "lucide-react";

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
    <div className="mx-auto w-full max-w-3xl min-w-0 flex-col gap-6 px-4 py-6 [overflow-wrap:anywhere] flex">
      <header className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[#0b3328] text-emerald-300">
          <ShieldCheck className="h-5 w-5" aria-hidden="true" />
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-emerald-700/70 dark:text-emerald-400/70">
            Audit report
          </p>
          <h1 className="text-2xl font-semibold tracking-tight text-[#0b3328] dark:text-emerald-50">
            Completeness proof
          </h1>
        </div>
      </header>
      <ProofPanel proof={proof} mode="full" counts={run.counts as Partial<Record<Outcome, number>>} />
    </div>
  );
}
