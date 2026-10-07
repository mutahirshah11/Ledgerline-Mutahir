// 006-web T032: the SCR-03 step model and planner (data-model.md §2 "Step", §3.1; research R8,
// R9; FR-011, FR-014) and the step / read-cost lines (contracts/ui-copy.md §3, "Read cost").
// Pure: no React, no fetch. Money stays a decimal string from the API (never a JS number).

import type { RunStatus, Stage } from "./wording";

export type StepState = "pending" | "running" | "done" | "failed";
export type StepKey = "upload" | "validate" | `page:${number}` | "reconcile";

export type StepError = { message: string; code: string | null };

export type Step = {
  key: StepKey;
  label: string;
  state: StepState;
  /** Status line (ui-copy.md §3), or null. */
  line: string | null;
  error: StepError | null;
  /** "retrying (attempt n of 3)" while running, from EP-19 RETRY events. */
  attempt: number | null;
  /** Whether "Retry this step" can help (the API has no retry for a run failed at upload or
   * validate: it must be created again, FR-024). */
  retryable: boolean;
};

/** The EP-05 fields the planner reads. */
export type PlanInput = {
  status: RunStatus;
  failed_stage: Stage | null;
  error_message: string | null;
  stage_progress: { pages: { page_no: number; extracted: boolean; cached: boolean }[] };
};

/** `next`: the step to run (or shown failed), or "overview" when the run is past reconcile. */
export type StepPlan = { steps: Step[]; next: StepKey | "overview" };

export function pageKey(n: number): StepKey {
  return `page:${n}`;
}

/** The page number of a `page:n` key, else null. */
export function pageOf(key: StepKey): number | null {
  return key.startsWith("page:") ? Number(key.slice(5)) : null;
}

function labelOf(key: StepKey): string {
  const page = pageOf(key);
  if (page !== null) return `Read page ${page}`;
  return { upload: "Upload", validate: "Validate", reconcile: "Reconcile" }[key as "upload"];
}

function step(key: StepKey): Step {
  return { key, label: labelOf(key), state: "pending", line: null, error: null, attempt: null, retryable: true };
}

/** Upload → Validate → Read page 1…n → Reconcile, all waiting. */
export function initialSteps(pages: number): Step[] {
  const keys: StepKey[] = ["upload", "validate"];
  for (let n = 1; n <= pages; n++) keys.push(pageKey(n));
  keys.push("reconcile");
  return keys.map(step);
}

const PAST_RECONCILE: ReadonlySet<RunStatus> = new Set(["RECONCILED", "AWAITING_REVIEW", "READY", "FINALISED"]);

/** data-model.md §3.1: which steps a run has done and which comes next. `pageCount` defaults to
 * the pages EP-05 knows (none before upload is confirmed: pass the dropped page count). */
export function planSteps(run: PlanInput, pageCount?: number): StepPlan {
  const pages = run.stage_progress.pages;
  const total = Math.max(pageCount ?? 0, pages.length);
  const steps = initialSteps(total);
  const read = new Set(pages.filter((p) => p.extracted).map((p) => p.page_no));
  const firstUnread = steps.find((s) => pageOf(s.key) !== null && !read.has(pageOf(s.key)!));

  const doneUntil = (key: StepKey | "overview", markReadPages: boolean) => {
    for (const s of steps) {
      if (s.key === key) break;
      const page = pageOf(s.key);
      if (page === null || !markReadPages || read.has(page)) s.state = "done";
    }
  };

  let next: StepKey | "overview";
  switch (run.status) {
    case "CREATED":
      next = "upload";
      break;
    case "UPLOADED":
      next = "validate";
      break;
    case "VALIDATED":
    case "EXTRACTING":
      next = firstUnread?.key ?? "reconcile";
      doneUntil("reconcile", true);
      return { steps, next };
    case "EXTRACTED":
      next = "reconcile";
      break;
    case "FAILED":
      return planFailed(run, steps, firstUnread?.key ?? "reconcile");
    default:
      if (!PAST_RECONCILE.has(run.status)) throw new Error(`Unknown run status ${run.status}`);
      next = "overview";
  }
  doneUntil(next, false);
  if (next === "overview") steps.forEach((s) => (s.state = "done"));
  return { steps, next };
}

function planFailed(run: PlanInput, steps: Step[], firstUnread: StepKey): StepPlan {
  const stage = run.failed_stage;
  const key: StepKey =
    stage === "upload" ? "upload" : stage === "validate" ? "validate" : stage === "extract" ? firstUnread : "reconcile";
  for (const s of steps) {
    if (s.key === key) {
      s.state = "failed";
      s.error = { message: run.error_message ?? "This step failed", code: null };
      s.retryable = key !== "upload" && key !== "validate";
      break;
    }
    s.state = "done";
  }
  return { steps, next: key };
}

// --- lines (contracts/ui-copy.md §3) ---------------------------------------------------------

const pagesWord = (n: number) => (n === 1 ? "page" : "pages");

export const stepLines = {
  uploading: (files: number) => `Uploading ${files} files`,
  uploaded: (fresh: number, known: number) => `${fresh} new ${pagesWord(fresh)}, ${known} already read`,
  reading: (n: number, total: number) => `Reading page ${n} of ${total}…`,
  readDone: (n: number, total: number, r: { cached: boolean; rows: number; review_cells: number }) =>
    r.cached
      ? `Reading page ${n} of ${total}… done (from cache)`
      : `Reading page ${n} of ${total}… done (new read, ${r.rows} rows, ${r.review_cells} cells need you)`,
  retrying: (attempt: number) => `retrying (attempt ${attempt} of 3)`,
  reconciled: "Reconciled",
  timeout: "Timed out. Try this step again.",
  fileChanged: "File changed during upload, upload it again",
  duplicate: "This exact month and files were already finalised",
  openThatRun: "Open that run",
} as const;

// --- read cost (spec Clarifications 2026-10-07; data-model.md "Read cost") ----------------------

export const READ_COST_UNAVAILABLE = "Read cost not available";
export const READ_COST_NONE = "US$0 · pages already read — no cost";

/** A decimal string that is zero ("0", "0.000"). String test only, never parsed to a number. */
function isZero(usd: string): boolean {
  return /^0*(\.0*)?$/.test(usd.trim()) && /0/.test(usd);
}

/** Before reading: "{n} new pages · est. US${estimate}" (estimate from the API, as received);
 * "US$0 · pages already read — no cost" when the estimate is zero; nothing misleading when the
 * prices are unset (null). `newPages` 0 with a non-zero estimate (a page the manifest knows but
 * whose reads are not stored) shows the estimate alone. */
export function readCostEstimateLine(newPages: number | null, estimateUsd: string | null): string {
  if (estimateUsd === null) return READ_COST_UNAVAILABLE;
  if (isZero(estimateUsd)) return READ_COST_NONE;
  if (!newPages) return `est. US$${estimateUsd}`;
  return `${newPages} new ${pagesWord(newPages)} · est. US$${estimateUsd}`;
}

/** After reading: "Read cost US${actual}" from EP-05 `read_cost.actual_usd`. */
export function readCostActualLine(actualUsd: string | null): string {
  return actualUsd === null ? READ_COST_UNAVAILABLE : `Read cost US$${actualUsd}`;
}

// --- EP-19 (research R9) --------------------------------------------------------------------------

export type EventLike = { stage: Stage; status: string; detail: Record<string, unknown> };

function stageOf(key: StepKey): Stage {
  return pageOf(key) !== null ? "extract" : (key as Stage);
}

/** The newest EP-19 event (events come newest first) for this step; its `attempt` when it is a
 * RETRY, else null. Page steps match on `detail.page_no`. */
export function latestRetryAttempt(events: readonly EventLike[], key: StepKey): number | null {
  const stage = stageOf(key);
  const page = pageOf(key);
  const latest = events.find((e) => e.stage === stage && (page === null || e.detail.page_no === page));
  if (!latest || latest.status !== "RETRY") return null;
  return typeof latest.detail.attempt === "number" ? latest.detail.attempt : null;
}
