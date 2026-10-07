"use client";

import Link from "next/link";
import { useState } from "react";

import { useMe } from "@/app/providers";
import { Banner } from "@/components/Banner";
import { ExceptionSections } from "@/components/ExceptionSections";
import { FinalisePanel, SummaryNotDelivered, useFinalise, type RunView } from "@/components/FinalisePanel";
import { ProofPanel, type Proof } from "@/components/ProofPanel";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import { stageText } from "@/lib/wording";

// SCR-04 body (006-web T042; FR-016..FR-022, FR-036). The server page reads EP-05 + EP-13 and
// hands them here; this client part keeps the run in state so EP-17's answer (finalise / resend)
// turns the screen read-only without a reload. Top to bottom (FR-016): headline, proof badge,
// banners, the five exception sections, Finalise or Report panel. Single column, wrapping text:
// usable at 380 px with no sideways page scroll (SCR-04-AC10).

export type RunOverviewProps = {
  initialRun: RunView;
  /** EP-13, or null when the run has no proof yet. */
  proof: Proof | null;
};

export function RunOverview({ initialRun, proof }: RunOverviewProps) {
  const { can } = useMe();
  const [run, setRun] = useState(initialRun);
  const finalise = useFinalise(run, setRun);
  const id = run.id;
  const failed = run.status === "FAILED";
  const actualCost = run.read_cost.actual_usd;

  return (
    <div className="mx-auto w-full max-w-3xl min-w-0 space-y-5 px-4 py-6 [overflow-wrap:anywhere]">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold [overflow-wrap:anywhere]">{run.headline}</h1>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          <RunStatusBadge status={run.status} failedStage={run.failed_stage} />
          <span>{run.month}</span>
          {actualCost !== null && <span>Read cost US${actualCost}</span>}
        </div>
        {proof && <ProofPanel proof={proof} />}
        <nav aria-label="Run pages" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
          <Link href={`/runs/${id}/results`} className="underline underline-offset-4">
            See all receipts
          </Link>
          <Link href={`/runs/${id}/proof`} className="underline underline-offset-4">
            Proof
          </Link>
          <Link href={`/runs/${id}/audit`} className="underline underline-offset-4">
            Audit
          </Link>
        </nav>
      </header>

      {run.error_message && (
        <Banner
          tone="error"
          title={run.error_message}
          action={
            failed && can.retry ? (
              <Link href={`/runs/new?run=${id}`} className="mt-2 inline-block font-medium underline underline-offset-4">
                Retry
              </Link>
            ) : undefined
          }
        >
          {failed && run.failed_stage && <p>Failed at {stageText(run.failed_stage)}.</p>}
        </Banner>
      )}
      <SummaryNotDelivered finalise={finalise} canResend={can.finalise} />

      <ExceptionSections
        exceptions={run.exceptions}
        runId={id}
        openReviewItems={run.open_review_items}
        readOnly={run.status === "FINALISED"}
      />

      <FinalisePanel
        run={run}
        proofStatus={proof?.status ?? run.proof_status}
        canFinalise={can.finalise}
        finalise={finalise}
      />
    </div>
  );
}
