import { RunOverview } from "@/components/RunOverview";
import { ErrorState } from "@/components/states";
import { ApiError } from "@/lib/api";
import { serverApi } from "@/lib/server-api";

// SCR-04 `/runs/[id]` run overview (006-web T042; contracts/screens.md SCR-04). Server read of
// EP-05 (the run) and EP-13 (the proof badge numbers, only once the run has a proof) with the
// session token; the client part (components/RunOverview.tsx) owns finalise / resend / downloads.
// 401 / 403 redirects come from serverApi(); anything else shows the plain error state.

function readProofOrNull(err: unknown): null {
  if (err instanceof ApiError) return null;
  throw err; // e.g. a redirect from serverApi()
}

export default async function RunOverviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const client = await serverApi({ next: `/runs/${id}` });
  let run;
  try {
    run = await client.get("/runs/{id}", { path: { id } });
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    return (
      <div className="px-4 py-6">
        <ErrorState error={err} />
      </div>
    );
  }
  const proof = run.proof_status
    ? await client.get("/runs/{id}/proof", { path: { id } }).catch(readProofOrNull)
    : null;
  return <RunOverview initialRun={run} proof={proof} />;
}
