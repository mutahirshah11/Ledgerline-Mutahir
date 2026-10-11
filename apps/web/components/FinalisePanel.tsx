"use client";

import { CheckCircle2, Download, FileText, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

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
import { formatTime } from "@/lib/time";

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
  /** Summary PENDING or FAILED on the run, or EP-17 answered 502: show the banner with "Resend". */
  notDelivered: boolean;
  /** Summary SENDING, and recently enough that it's still plausibly in flight. */
  summarySending: boolean;
  /** Summary SENDING, but not updated recently: likely stuck: say so instead of "being sent". */
  summaryStale: boolean;
  /** EP-17 answered 409 PROOF_NOT_PASSED. */
  proofRefused: boolean;
  /** Any other EP-17 failure, shown in the panel. */
  error: ApiError | null;
  /** EP-17 `{confirm: true}`: finalise, or resend the summary of a finalised run. */
  send: () => Promise<void>;
};

/** No precedent elsewhere in the app for "how long is too long" on a stuck SENDING summary
 * (lib/run-steps.ts's retry/timeout machinery is about EP-19 stage events, a different thing);
 * 5 minutes is this component's own choice — long enough that an ordinary Telegram round trip
 * never trips it, short enough to tell the controller something needs a look. */
const STALE_SENDING_MS = 5 * 60 * 1000;

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
      else {
        setError(e);
        // FR-020: a resend that fails for any other reason (e.g. 409 RUN_STATE_CONFLICT) still
        // reflects whatever the API now has for this run, fetched fresh from the browser (not a
        // server redirect) rather than trusting the stale `run` this hook was called with.
        try {
          onRun(await api.get("/runs/{id}", { path: { id: runId } }));
        } catch {
          // Best-effort: the error above already tells the controller something went wrong.
        }
      }
    } finally {
      setSending(false);
    }
  }, [runId, onRun]);

  const summary = run.summary;
  const summaryUpdated = summary?.status === "SENDING" ? Date.parse(summary.updated_at) : NaN;
  const now = Date.parse(run.scenario_now);
  const summaryStale =
    summary?.status === "SENDING" && !Number.isNaN(summaryUpdated) && !Number.isNaN(now)
      ? now - summaryUpdated > STALE_SENDING_MS
      : false;

  return {
    sending,
    notDelivered: upstreamFailed || summary?.status === "FAILED" || summary?.status === "PENDING",
    summarySending: summary?.status === "SENDING" && !summaryStale,
    summaryStale,
    proofRefused,
    error,
    send,
  };
}

/** "08:31" (Asia/Karachi), regardless of the ISO instant's own offset. */
export const hhmm = formatTime;

export type SummaryNotDeliveredProps = {
  finalise: FinaliseState;
  /** CONTROLLER only (EP-17). */
  canResend: boolean;
};

/** ui-copy.md §4 "Not delivered": amber banner + "Resend" (SCR-04-AC6, FM-11). Also covers the
 * two other summary states FR-020 calls out, neither of which offers Resend: a fresh SENDING
 * ("still in flight, don't pile on another attempt") and a stale one ("this may actually be
 * stuck or have already gone through — look before resending"). */
export function SummaryNotDelivered({ finalise, canResend }: SummaryNotDeliveredProps) {
  if (finalise.notDelivered) {
    return (
      <Banner
        tone="warning"
        title="Summary not delivered"
        action={
          canResend ? (
            <Button className="mt-2" onClick={() => void finalise.send()} disabled={finalise.sending}>
              <Download className="h-4 w-4" aria-hidden="true" />
              Resend
            </Button>
          ) : undefined
        }
      />
    );
  }
  if (finalise.summaryStale) {
    return <Banner tone="warning" title="Summary may have been delivered — check Telegram before resending" />;
  }
  if (finalise.summarySending) {
    return <Banner tone="info" title="Summary is being sent" />;
  }
  return null;
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
  // base-ui's alert dialog unmounts asynchronously (it waits a frame to check for a running CSS
  // animation via `element.getAnimations()`, even with none declared), so `open` can already be
  // false while the dialog's own "Finalise and send" button is still in the DOM — the same
  // accessible name as the trigger below. `closing` covers that gap so the two are never shown
  // at once (root cause of a strict-mode violation in FR-019's tests; see `onOpenChangeComplete`).
  const [closing, setClosing] = useState(false);
  const handleOpenChange = useCallback((next: boolean) => {
    setOpen(next);
    if (!next) setClosing(true);
  }, []);
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

  const reportHeadingRef = useRef<HTMLHeadingElement>(null);
  const wasFinalisedRef = useRef(run.status === "FINALISED");
  useEffect(() => {
    if (run.status === "FINALISED" && !wasFinalisedRef.current) reportHeadingRef.current?.focus();
    wasFinalisedRef.current = run.status === "FINALISED";
  }, [run.status]);

  if (run.status === "FINALISED") {
    const summary = run.summary;
    return (
      <section
        aria-labelledby="finalise-heading"
        className="space-y-3 rounded-xl border border-l-4 border-l-emerald-500 bg-card p-5 shadow-sm"
      >
        <div className="flex items-center gap-2.5">
          <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" aria-hidden="true" />
          <h2
            id="finalise-heading"
            ref={reportHeadingRef}
            tabIndex={-1}
            className="text-lg font-semibold tracking-tight outline-none"
          >
            Report
          </h2>
        </div>
        <p className="text-sm text-muted-foreground">This run is finalised and read-only.</p>
        {summary?.status === "SENT" && summary.sent_at && (
          <p
            role="status"
            className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-sm font-medium text-emerald-800 [overflow-wrap:anywhere] dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
          >
            Finalised. Summary sent to the controller at {hhmm(summary.sent_at)}
          </p>
        )}
        {finalise.error && <p role="alert">{errorStateText(finalise.error)}</p>}
        <div className="flex flex-wrap gap-2 pt-1">
          <Button variant="outline" disabled={downloading !== null} onClick={() => void download("report")}>
            <FileText className="h-4 w-4" aria-hidden="true" />
            Download report
          </Button>
          <Button variant="outline" disabled={downloading !== null} onClick={() => void download("csv")}>
            <Download className="h-4 w-4" aria-hidden="true" />
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
      : `${n} review item${n === 1 ? " is" : "s are"} still open. Finalise and send the summary now?`;

  return (
    <section
      aria-labelledby="finalise-heading"
      className="space-y-3 rounded-xl border border-l-4 border-l-[#0b3328] bg-card p-5 shadow-sm dark:border-l-emerald-500"
    >
      <div className="flex items-center gap-2.5">
        <ShieldCheck className="h-5 w-5 shrink-0 text-[#0b3328] dark:text-emerald-400" aria-hidden="true" />
        <h2 id="finalise-heading" className="text-lg font-semibold tracking-tight">
          Finalise
        </h2>
      </div>
      {!open && !closing && (
        <Button
          disabled={blocked || finalise.sending}
          aria-describedby={blocked ? "finalise-reason" : undefined}
          onClick={() => setOpen(true)}
          size="lg"
          className="max-w-full bg-[#0b3328] hover:bg-[#0b3328]/90"
        >
          Finalise and send
        </Button>
      )}
      {blocked && (
        <p id="finalise-reason" className="text-sm text-muted-foreground">
          {PROOF_NOT_PASSED_REASON}
        </p>
      )}
      {finalise.error && <p role="alert">{errorStateText(finalise.error)}</p>}
      <AlertDialog open={open} onOpenChange={handleOpenChange} onOpenChangeComplete={(next) => { if (!next) setClosing(false); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Finalise this run?</AlertDialogTitle>
            <AlertDialogDescription>{question}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                handleOpenChange(false);
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
