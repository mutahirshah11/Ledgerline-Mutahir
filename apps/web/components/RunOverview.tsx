"use client";

import { Receipt, ScrollText, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { useMe } from "@/app/providers";
import { Banner } from "@/components/Banner";
import { ExceptionSections } from "@/components/ExceptionSections";
import { FinalisePanel, SummaryNotDelivered, useFinalise, type RunView } from "@/components/FinalisePanel";
import { ProofPanel, ProofStatusBadge, type Proof } from "@/components/ProofPanel";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import { Button } from "@/components/ui/button";
import { stageText } from "@/lib/wording";

// FR-022: statuses before AWAITING_REVIEW/READY (and short of FINALISED/FAILED) are still being
// built by SCR-03's step runner; this page has nothing of theirs yet to show, so it points back
// there instead of rendering an empty overview (lib/run-steps.ts's own `PAST_RECONCILE` boundary,
// read the other way round).
const IN_PROGRESS_STATUSES: ReadonlySet<RunView["status"]> = new Set([
  "CREATED",
  "UPLOADED",
  "VALIDATED",
  "EXTRACTING",
  "EXTRACTED",
  "RECONCILED",
]);

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
  const inProgress = IN_PROGRESS_STATUSES.has(run.status);
  const actualCost = run.read_cost.actual_usd;
  const stageLine = failed && run.failed_stage ? `Failed at ${stageText(run.failed_stage)}.` : null;
  // FR-022: a FAILED run can carry `error_message: null`; still show *something* to act on.
  const failureTitle = run.error_message ?? stageLine;

  return (
    <div className="mx-auto w-full max-w-3xl min-w-0 space-y-6 px-4 py-6 [overflow-wrap:anywhere]">
      <header className="space-y-3">
        <div>
          <p className="text-xs font-medium tracking-wide text-emerald-700/70 uppercase dark:text-emerald-400/70">
            Run overview
          </p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-[#0b3328] [overflow-wrap:anywhere] sm:text-3xl dark:text-emerald-50">
            {run.headline}
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          <RunStatusBadge status={run.status} failedStage={run.failed_stage} />
          <span>{run.month}</span>
          {actualCost !== null && <span>Read cost US${actualCost}</span>}
        </div>
        {proof ? (
          <ProofPanel proof={proof} />
        ) : (
          run.proof_status && (
            <ProofStatusBadge
              status={run.proof_status}
              text={run.proof_status === "PASS" ? "Proof passed" : "Proof failed"}
            />
          )
        )}
        <nav aria-label="Run pages" className="flex flex-wrap gap-2 text-sm">
          <Button variant="outline" size="sm" render={<Link href={`/runs/${id}/results`} />}>
            <Receipt className="h-4 w-4" aria-hidden="true" />
            See all receipts
          </Button>
          <Button variant="outline" size="sm" render={<Link href={`/runs/${id}/proof`} />}>
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            Proof
          </Button>
          <Button variant="outline" size="sm" render={<Link href={`/runs/${id}/audit`} />}>
            <ScrollText className="h-4 w-4" aria-hidden="true" />
            Audit
          </Button>
        </nav>
      </header>

      {inProgress && (
        <Banner
          tone="info"
          title="This run is still in progress"
          action={
            <Link href={`/runs/new?run=${id}`} className="mt-2 inline-block font-medium underline underline-offset-4">
              Continue this run
            </Link>
          }
        />
      )}
      {failureTitle && (
        <Banner
          tone="error"
          title={failureTitle}
          action={
            failed && can.retry ? (
              <Link href={`/runs/new?run=${id}`} className="mt-2 inline-block font-medium underline underline-offset-4">
                Retry
              </Link>
            ) : undefined
          }
        >
          {/* `stageLine` already carries the title when there is no separate error message
           * (FR-022: a FAILED run can have `error_message: null`); don't repeat it. */}
          {run.error_message && stageLine && <p>{stageLine}</p>}
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
