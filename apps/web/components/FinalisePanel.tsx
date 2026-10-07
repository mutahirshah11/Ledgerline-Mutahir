"use client";

import { useCallback, useState } from "react";

import { Banner } from "@/components/Banner";
import { errorStateText } from "@/components/states";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { components } from "@/lib/api-types";

// contracts/components.md "FinalisePanel", data-model.md §3.3 (006-web T041; FR-019..FR-021,
// SCR-04-AC5..AC7, AC9). The EP-17 state machine lives in `useFinalise` so the overview can show
// the "Summary not delivered" banner near the top (contracts/screens.md layout) and the panel at
// the bottom from one state:
//
//   ready ──"Finalise and send"──▶ confirming ──confirm──▶ sending
//     sending ──200 SENT──▶ sent · ──200 FAILED / 502──▶ notDelivered ──Resend──▶ sending
//     sending ──409 PROOF_NOT_PASSED──▶ ready (disabled, reason)
//
// Downloads ask EP-18 for fresh signed links only when clicked (never prefetched) and open the
// link in a new tab.

export type RunView = components["schemas"]["RunView"];
export type ProofStatus = components["schemas"]["ProofStatus"];

export const PROOF_NOT_PASSED_REASON = "Proof has not passed";

export type FinaliseState = {
  sending: boolean;
  /** Summary FAILED on the run, or EP-17 answered 502: show the banner with "Resend". */
  notDelivered: boolean;
  /** EP-17 answered 409 PROOF_NOT_PASSED. */
  proofRefused: boolean;
  /** Any other EP-17 failure, shown in the panel. */
  error: ApiError | null;
  /** EP-17 `{confirm: true}`: finalise, or resend the summary of a finalised run. */
  send: () => Promise<void>;
};

export function useFinalise(run: RunView, onRun: (run: RunView) => void): FinaliseState {
  const [sending, setSending] = useState(false);
  const [upstreamFailed, setUpstreamFailed] = useState(false);
  const [proofRefused, setProofRefused] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const runId = run.id;

  const send = useCallback(async () => {
    setSending(true);
    setError(null);
    try {
      const result = await api.post("/runs/{id}/finalise", { path: { id: runId }, body: { confirm: true } });
      setUpstreamFailed(false);
      onRun(result.run);
    } catch (err) {
      const e =
        err instanceof ApiError ? err : new ApiError({ status: 0, code: "NETWORK_ERROR", message: String(err) });
      if (e.code === "PROOF_NOT_PASSED") setProofRefused(true);
      else if (e.status === 502 && e.code === "UPSTREAM_FAILED") setUpstreamFailed(true);
      else setError(e);
    } finally {
      setSending(false);
    }
  }, [runId, onRun]);

  return {
    sending,
    notDelivered: upstreamFailed || run.summary?.status === "FAILED",
    proofRefused,
    error,
    send,
  };
}

/** "08:31" from an ISO time such as "2026-10-15T08:31:30+05:00": the wall-clock time in the API's
 * own offset (the business time zone), the same on the server and in any browser. */
export function hhmm(iso: string): string {
  const m = iso.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : iso;
}

export type SummaryNotDeliveredProps = {
  finalise: FinaliseState;
  /** CONTROLLER only (EP-17). */
  canResend: boolean;
};

/** ui-copy.md §4 "Not delivered": amber banner + "Resend" (SCR-04-AC6, FM-11). */
export function SummaryNotDelivered({ finalise, canResend }: SummaryNotDeliveredProps) {
  if (!finalise.notDelivered) return null;
  return (
    <Banner
      tone="warning"
      title="Summary not delivered"
      action={
        canResend ? (
          <Button className="mt-2" onClick={() => void finalise.send()} disabled={finalise.sending}>
            Resend
          </Button>
        ) : undefined
      }
    />
  );
}

export type FinalisePanelProps = {
  run: RunView;
  /** EP-13 status (or EP-05 `proof_status`); "Finalise and send" needs PASS. */
  proofStatus: ProofStatus | null;
  /** useMe().can.finalise (CONTROLLER). */
  canFinalise: boolean;
  finalise: FinaliseState;
};

type Download = "report" | "csv";

export function FinalisePanel({ run, proofStatus, canFinalise, finalise }: FinalisePanelProps) {
  const [open, setOpen] = useState(false);
  const [downloading, setDownloading] = useState<Download | null>(null);
  const [downloadError, setDownloadError] = useState<ApiError | null>(null);

  async function download(kind: Download) {
    setDownloading(kind);
    setDownloadError(null);
    try {
      const links = await api.get("/runs/{id}/report", { path: { id: run.id } });
      window.open(kind === "report" ? links.html_url : links.csv_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      if (err instanceof ApiError) setDownloadError(err);
    } finally {
      setDownloading(null);
    }
  }

  if (run.status === "FINALISED") {
    const summary = run.summary;
    return (
      <section aria-labelledby="finalise-heading" className="space-y-3 rounded-lg border p-4">
        <h2 id="finalise-heading" className="text-lg font-semibold">
          Report
        </h2>
        <p>This run is finalised and read-only.</p>
        {summary?.status === "SENT" && summary.sent_at && (
          <p role="status">Finalised. Summary sent to the controller at {hhmm(summary.sent_at)}</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={downloading !== null} onClick={() => void download("report")}>
            Download report
          </Button>
          <Button variant="outline" disabled={downloading !== null} onClick={() => void download("csv")}>
            Download outcomes CSV
          </Button>
        </div>
        {downloadError && <p role="alert">{errorStateText(downloadError)}</p>}
      </section>
    );
  }

  const finalisable = run.status === "AWAITING_REVIEW" || run.status === "READY";
  if (!canFinalise || !finalisable) return null;

  const blocked = proofStatus !== "PASS" || finalise.proofRefused;
  const n = run.open_review_items;
  const question =
    n === 0
      ? "All review items are resolved. Finalise and send the summary now?"
      : `${n} review items are still open. Finalise and send the summary now?`;

  return (
    <section aria-labelledby="finalise-heading" className="space-y-3 rounded-lg border p-4">
      <h2 id="finalise-heading" className="text-lg font-semibold">
        Finalise
      </h2>
      <Button
        disabled={blocked || finalise.sending}
        aria-describedby={blocked ? "finalise-reason" : undefined}
        onClick={() => setOpen(true)}
        className="max-w-full"
      >
        Finalise and send
      </Button>
      {blocked && (
        <p id="finalise-reason" className="text-sm text-muted-foreground">
          {PROOF_NOT_PASSED_REASON}
        </p>
      )}
      {finalise.error && <p role="alert">{errorStateText(finalise.error)}</p>}
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Finalise this run?</AlertDialogTitle>
            <AlertDialogDescription>{question}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setOpen(false);
                void finalise.send();
              }}
            >
              Finalise and send
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
