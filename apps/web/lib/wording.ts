/**
 * Plain-language wording table (006-web T015). Every string here comes verbatim from
 * `specs/006-web/contracts/ui-copy.md` (cited per section below). Nothing on screen composes
 * its own wording from a code (F-09.R2) except the few parameterised lines the functions below
 * build exactly as ui-copy.md specifies (e.g. "{Outlier} is different").
 *
 * Enum types are re-exported from the generated `lib/api-types.ts` wherever the OpenAPI exposes
 * them (Source, Outcome, FlagType, RunStatus, Stage, CellStatus, ReviewReason, ProblemCode,
 * AuditAction). `ErrorCode` is NOT in the generated OpenAPI (no `ErrorCode` schema in
 * `apps/web/lib/api-types.ts` as of this writing) — it is defined below from
 * `apps/api/app/domain/enums.py::ErrorCode` instead, per the 006-web common rules.
 */
import type { components } from "./api-types";

export type Source = components["schemas"]["Source"];
export type Outcome = components["schemas"]["Outcome"];
export type FlagType = components["schemas"]["FlagType"];
export type RunStatus = components["schemas"]["RunStatus"];
export type Stage = components["schemas"]["Stage"];
export type CellStatus = components["schemas"]["CellStatus"];
export type ReviewReason = components["schemas"]["ReviewReason"];
export type ProblemCode = components["schemas"]["ProblemCode"];
export type AuditAction = components["schemas"]["AuditAction"];

/** Not in the generated OpenAPI — see module doc comment above. */
export type ErrorCode =
  | "BAD_MONTH"
  | "MISSING_SOURCE"
  | "BAD_REQUEST"
  | "CONFIRM_REQUIRED"
  | "UNAUTHENTICATED"
  | "NOT_ALLOWED"
  | "NOT_FOUND"
  | "RUN_STATE_CONFLICT"
  | "DUPLICATE_RUN"
  | "PROOF_NOT_PASSED"
  | "ITEM_ALREADY_RESOLVED"
  | "CONTRACT_VIOLATION"
  | "INVALID_REVIEW_ACTION"
  | "UPSTREAM_FAILED"
  | "INTERNAL_ERROR";

// ---------------------------------------------------------------------------------------------
// §1 Sources, outcomes, flags
// ---------------------------------------------------------------------------------------------

const SOURCE_LABEL: Record<Source, string> = {
  REGISTER: "Register",
  ERP: "ERP",
  SHEET: "Store sheet",
};

export function sourceLabel(source: Source): string {
  return SOURCE_LABEL[source];
}

/** Joins source labels the way ui-copy.md §1 MISSING does: "Register and Store sheet". */
export function joinSources(sources: Source[]): string {
  const labels = sources.map(sourceLabel);
  if (labels.length === 0) return "";
  if (labels.length === 1) return labels[0];
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

const OUTCOME_LABEL: Record<Outcome, string> = {
  MATCHED: "All three agree",
  MATCHED_AFTER_CONVERSION: "Agree after unit conversion",
  // MISMATCHED / MISSING are parameterised — see outcomeLabel() below. The bare strings here are
  // only the no-parameter fallback (outcomeLabel always calls through the branches instead).
  MISMATCHED: "Sources disagree",
  MISSING: "Missing from",
  NEEDS_REVIEW: "Needs review",
  UNMATCHED_NO_KEY: "No GRN to match on",
  DUPLICATE: "Duplicate row",
  NON_RECEIPT: "Not a receipt (return)",
};

export type OutcomeLabelOpts = {
  /** MISMATCHED only: the differing source, or null/undefined for "Sources disagree". */
  outlier?: Source | null;
  /** MISSING only: the sources the row is absent from. */
  missingFrom?: Source[];
};

export function outcomeLabel(outcome: Outcome, opts?: OutcomeLabelOpts): string {
  if (outcome === "MISMATCHED") {
    const outlier = opts?.outlier;
    return outlier ? `${sourceLabel(outlier)} is different` : "Sources disagree";
  }
  if (outcome === "MISSING") {
    return `Missing from ${joinSources(opts?.missingFrom ?? [])}`;
  }
  return OUTCOME_LABEL[outcome];
}

const FLAG_LABEL: Record<FlagType, string> = {
  SEQUENCE_GAP: "GRN number missing from every record",
  BACKDATED_ENTRY: "Entered late and back-dated",
  UNUSUAL_USER: "Entered by an unusual user",
  SUNDAY_DATE: "Dated on a Sunday",
  OUT_OF_ORDER: "Date out of order",
  DATE_OUT_OF_PERIOD: "Date outside the month",
  UNKNOWN_SUPPLIER: "Supplier name not recognised",
  UNKNOWN_UNIT: "Unit not recognised",
};

export function flagLabel(flag: FlagType): string {
  return FLAG_LABEL[flag];
}

const CELL_STATUS_LABEL: Record<CellStatus, string> = {
  CLEAR: "clear",
  UNCLEAR: "unclear",
  CORRECTED: "corrected",
  ILLEGIBLE: "unreadable",
};

export function cellStatusLabel(status: CellStatus): string {
  return CELL_STATUS_LABEL[status];
}

// ---------------------------------------------------------------------------------------------
// §2 Run statuses and stages
// ---------------------------------------------------------------------------------------------

const RUN_STATUS_LABEL: Record<Exclude<RunStatus, "FAILED">, string> = {
  CREATED: "Created",
  UPLOADED: "Files uploaded",
  VALIDATED: "Files checked",
  EXTRACTING: "Reading pages",
  EXTRACTED: "Pages read",
  RECONCILED: "Reconciled",
  AWAITING_REVIEW: "Waiting on review",
  READY: "Ready to finalise",
  FINALISED: "Finalised",
};

/** The gerund/noun phrase used in "Failed at {stage text}." (ui-copy.md §2 "Stages" list). */
const STAGE_TEXT: Record<Stage, string> = {
  upload: "uploading files",
  validate: "checking files",
  extract: "reading pages",
  reconcile: "reconciling",
  proof: "the completeness proof",
  review: "review",
  finalise: "finalising",
  notify: "sending the summary",
};

export function stageText(stage: Stage): string {
  return STAGE_TEXT[stage];
}

export function runStatusLabel(status: RunStatus, failedStage?: Stage | null): string {
  if (status === "FAILED") {
    return failedStage ? `Failed at ${stageText(failedStage)}` : "Failed";
  }
  return RUN_STATUS_LABEL[status];
}

// ---------------------------------------------------------------------------------------------
// §5 Review reasons (also reused for the three flag-shaped reasons, per ui-copy.md "as §1")
// ---------------------------------------------------------------------------------------------

const REVIEW_REASON_LABEL: Record<ReviewReason, string> = {
  ILLEGIBLE: "unreadable on the register",
  CORRECTED: "corrected on the register",
  UNCLEAR: "unclear on the register",
  READS_DISAGREE: "the two reads disagree",
  ROW_IN_ONE_READ: "row seen in only one read",
  PAGE_LABEL_MISMATCH: "page number or month differs from the upload",
  UNKNOWN_SUPPLIER: FLAG_LABEL.UNKNOWN_SUPPLIER,
  UNKNOWN_UNIT: FLAG_LABEL.UNKNOWN_UNIT,
  UNPARSEABLE_DATE: "date could not be read",
  UNMATCHED_NO_KEY: "row has no GRN and no single match",
  CONFLICTING_ROWS_IN_SOURCE: "two different rows in one record",
  SEQUENCE_GAP: FLAG_LABEL.SEQUENCE_GAP,
  BACKDATED_ENTRY: FLAG_LABEL.BACKDATED_ENTRY,
  DATE_OUT_OF_PERIOD: FLAG_LABEL.DATE_OUT_OF_PERIOD,
};

export function reviewReasonLabel(reason: ReviewReason): string {
  return REVIEW_REASON_LABEL[reason];
}

// ---------------------------------------------------------------------------------------------
// §7 Audit actions
// ---------------------------------------------------------------------------------------------

const AUDIT_ACTION_LABEL: Record<AuditAction, string> = {
  RUN_CREATED: "Run created",
  FILE_UPLOADED: "File uploaded",
  RUN_VALIDATED: "Files checked",
  PAGE_EXTRACTED: "Page read",
  RECONCILED: "Reconciled",
  REVIEW_RESOLVED: "Review decision",
  RUN_FINALISED: "Run finalised",
  SUMMARY_SENT: "Summary sent",
  SUMMARY_FAILED: "Summary not delivered",
  ALERT_SENT: "Ops alert sent",
  RUN_DELETED: "Run deleted",
};

export function auditActionLabel(action: AuditAction): string {
  return AUDIT_ACTION_LABEL[action];
}

// ---------------------------------------------------------------------------------------------
// ProblemCode — GAP: ui-copy.md has no table for these (EP-08 `ValidationResult.problems[].message`
// is shown verbatim from the API instead, ui-copy.md §3 "validate failed"). These fallback strings
// are a best-effort completeness label only (e.g. for debugging/logging); do not render them as
// the validation message on SCR-03 — use the API's `message` field verbatim there.
// ---------------------------------------------------------------------------------------------

const PROBLEM_CODE_LABEL: Record<ProblemCode, string> = {
  ENCODING: "File encoding not recognised",
  MISSING_COLUMN: "A required column is missing",
  NO_DATA_ROWS: "No data rows found",
  BAD_NUMBER: "A number could not be read",
  BAD_DATE: "A date could not be read",
  MISSING_WORKSHEET: "Worksheet not found",
  MISSING_HEADER: "Header row not found",
  IMAGE_UNREADABLE: "Image could not be read",
  IMAGE_TOO_SMALL: "Image is too small",
  TOO_MANY_PAGES: "Too many pages",
};

export function problemCodeLabel(code: ProblemCode): string {
  return PROBLEM_CODE_LABEL[code];
}

// ---------------------------------------------------------------------------------------------
// ErrorCode — GAP: ui-copy.md has no single table for this either. Codes with a clear anchor
// elsewhere in ui-copy.md / spec.md use that exact text (cited per entry); the rest are
// best-effort fallback strings only, since nothing in the contracts fixes their wording — render
// the API's verbatim `message` for those instead wherever one is available (ErrorState in
// components/states.tsx does this).
// ---------------------------------------------------------------------------------------------

const ERROR_CODE_LABEL: Record<ErrorCode, string> = {
  BAD_MONTH: "Month is not valid",
  MISSING_SOURCE: "A required file is missing",
  BAD_REQUEST: "Request was not valid",
  CONFIRM_REQUIRED: "Confirm the upload before continuing",
  UNAUTHENTICATED: "Sign in to continue",
  // spec.md line 89 / ui-copy.md §7 "403 page" (B, 24).
  NOT_ALLOWED: "Your email is not on the list for this app",
  // ui-copy.md §7 "Not found".
  NOT_FOUND: "Run not found",
  // ui-copy.md §4/§5 "Read-only" / spec.md FR-013 (409 on a finalised run).
  RUN_STATE_CONFLICT: "This run is finalised and read-only.",
  // ui-copy.md §3 "DUPLICATE_RUN" (the lead line; "Open that run" is the separate action text).
  DUPLICATE_RUN: "This exact month and files were already finalised",
  // ui-copy.md §4 "Disabled reason".
  PROOF_NOT_PASSED: "Proof has not passed",
  ITEM_ALREADY_RESOLVED: "This item has already been decided",
  CONTRACT_VIOLATION: "Something unexpected happened",
  INVALID_REVIEW_ACTION: "This action is not available for this item",
  UPSTREAM_FAILED: "A step failed",
  INTERNAL_ERROR: "Something went wrong",
};

export function errorCodeLabel(code: ErrorCode): string {
  return ERROR_CODE_LABEL[code];
}
