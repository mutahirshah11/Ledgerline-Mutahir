import { expect, test } from "@playwright/test";
import {
  auditActionLabel,
  cellStatusLabel,
  errorCodeLabel,
  flagLabel,
  outcomeLabel,
  problemCodeLabel,
  reviewReasonLabel,
  runStatusLabel,
  sourceLabel,
  stageText,
  type AuditAction,
  type CellStatus,
  type ErrorCode,
  type FlagType,
  type Outcome,
  type ProblemCode,
  type ReviewReason,
  type RunStatus,
  type Source,
  type Stage,
} from "../../lib/wording";

// T011 (006-web Phase 2): every enum value used on screen has a plain-language string, and the
// strings match specs/006-web/contracts/ui-copy.md verbatim (where ui-copy.md defines one).

const SOURCES: Source[] = ["REGISTER", "ERP", "SHEET"];
const OUTCOMES: Outcome[] = [
  "MATCHED",
  "MATCHED_AFTER_CONVERSION",
  "MISMATCHED",
  "MISSING",
  "NEEDS_REVIEW",
  "UNMATCHED_NO_KEY",
  "DUPLICATE",
  "NON_RECEIPT",
];
const FLAGS: FlagType[] = [
  "SEQUENCE_GAP",
  "BACKDATED_ENTRY",
  "UNUSUAL_USER",
  "SUNDAY_DATE",
  "OUT_OF_ORDER",
  "DATE_OUT_OF_PERIOD",
  "UNKNOWN_SUPPLIER",
  "UNKNOWN_UNIT",
];
const RUN_STATUSES: RunStatus[] = [
  "CREATED",
  "UPLOADED",
  "VALIDATED",
  "EXTRACTING",
  "EXTRACTED",
  "RECONCILED",
  "AWAITING_REVIEW",
  "READY",
  "FINALISED",
  "FAILED",
];
const STAGES: Stage[] = [
  "upload",
  "validate",
  "extract",
  "reconcile",
  "proof",
  "review",
  "finalise",
  "notify",
];
const CELL_STATUSES: CellStatus[] = ["CLEAR", "UNCLEAR", "CORRECTED", "ILLEGIBLE"];
const REVIEW_REASONS: ReviewReason[] = [
  "ILLEGIBLE",
  "CORRECTED",
  "UNCLEAR",
  "READS_DISAGREE",
  "ROW_IN_ONE_READ",
  "PAGE_LABEL_MISMATCH",
  "UNKNOWN_SUPPLIER",
  "UNKNOWN_UNIT",
  "UNPARSEABLE_DATE",
  "UNMATCHED_NO_KEY",
  "CONFLICTING_ROWS_IN_SOURCE",
  "SEQUENCE_GAP",
  "BACKDATED_ENTRY",
  "DATE_OUT_OF_PERIOD",
];
const PROBLEM_CODES: ProblemCode[] = [
  "ENCODING",
  "MISSING_COLUMN",
  "NO_DATA_ROWS",
  "BAD_NUMBER",
  "BAD_DATE",
  "MISSING_WORKSHEET",
  "MISSING_HEADER",
  "IMAGE_UNREADABLE",
  "IMAGE_TOO_SMALL",
  "TOO_MANY_PAGES",
];
const ERROR_CODES: ErrorCode[] = [
  "BAD_MONTH",
  "MISSING_SOURCE",
  "BAD_REQUEST",
  "CONFIRM_REQUIRED",
  "UNAUTHENTICATED",
  "NOT_ALLOWED",
  "NOT_FOUND",
  "RUN_STATE_CONFLICT",
  "DUPLICATE_RUN",
  "PROOF_NOT_PASSED",
  "ITEM_ALREADY_RESOLVED",
  "CONTRACT_VIOLATION",
  "INVALID_REVIEW_ACTION",
  "UPSTREAM_FAILED",
  "INTERNAL_ERROR",
];
const AUDIT_ACTIONS: AuditAction[] = [
  "RUN_CREATED",
  "FILE_UPLOADED",
  "RUN_VALIDATED",
  "PAGE_EXTRACTED",
  "RECONCILED",
  "REVIEW_RESOLVED",
  "RUN_FINALISED",
  "SUMMARY_SENT",
  "SUMMARY_FAILED",
  "ALERT_SENT",
  "RUN_DELETED",
];

test("every Source has a non-empty string", () => {
  for (const s of SOURCES) expect(sourceLabel(s).length).toBeGreaterThan(0);
});

test("Source strings match ui-copy.md §1", () => {
  expect(sourceLabel("REGISTER")).toBe("Register");
  expect(sourceLabel("ERP")).toBe("ERP");
  expect(sourceLabel("SHEET")).toBe("Store sheet");
});

test("every Outcome has a non-empty string", () => {
  for (const o of OUTCOMES) expect(outcomeLabel(o).length).toBeGreaterThan(0);
});

test("Outcome strings match ui-copy.md §1 / data-model.md §4", () => {
  expect(outcomeLabel("MATCHED")).toBe("All three agree");
  expect(outcomeLabel("MATCHED_AFTER_CONVERSION")).toBe("Agree after unit conversion");
  expect(outcomeLabel("NEEDS_REVIEW")).toBe("Needs review");
  expect(outcomeLabel("UNMATCHED_NO_KEY")).toBe("No GRN to match on");
  expect(outcomeLabel("DUPLICATE")).toBe("Duplicate row");
  expect(outcomeLabel("NON_RECEIPT")).toBe("Not a receipt (return)");
});

test("MISMATCHED with outlier ERP reads 'ERP is different'", () => {
  expect(outcomeLabel("MISMATCHED", { outlier: "ERP" })).toBe("ERP is different");
});

test("MISMATCHED with no outlier reads 'Sources disagree'", () => {
  expect(outcomeLabel("MISMATCHED", { outlier: null })).toBe("Sources disagree");
  expect(outcomeLabel("MISMATCHED")).toBe("Sources disagree");
});

test("MISSING joins missingFrom with 'and'", () => {
  expect(outcomeLabel("MISSING", { missingFrom: ["REGISTER", "SHEET"] })).toBe(
    "Missing from Register and Store sheet",
  );
});

test("every FlagType has a non-empty string matching ui-copy.md §1", () => {
  const expected: Record<FlagType, string> = {
    SEQUENCE_GAP: "GRN number missing from every record",
    BACKDATED_ENTRY: "Entered late and back-dated",
    UNUSUAL_USER: "Entered by an unusual user",
    SUNDAY_DATE: "Dated on a Sunday",
    OUT_OF_ORDER: "Date out of order",
    DATE_OUT_OF_PERIOD: "Date outside the month",
    UNKNOWN_SUPPLIER: "Supplier name not recognised",
    UNKNOWN_UNIT: "Unit not recognised",
  };
  for (const f of FLAGS) expect(flagLabel(f)).toBe(expected[f]);
});

test("every RunStatus has a non-empty string matching ui-copy.md §2", () => {
  const expected: Partial<Record<RunStatus, string>> = {
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
  for (const s of RUN_STATUSES) {
    if (s === "FAILED") continue;
    expect(runStatusLabel(s)).toBe(expected[s]);
  }
});

test("FAILED run status shows 'Failed at {stage text}'", () => {
  expect(runStatusLabel("FAILED", "extract")).toBe("Failed at reading pages");
  expect(runStatusLabel("FAILED", "finalise")).toBe("Failed at finalising");
});

test("every Stage has a non-empty descriptive string per ui-copy.md §2", () => {
  const expected: Record<Stage, string> = {
    upload: "uploading files",
    validate: "checking files",
    extract: "reading pages",
    reconcile: "reconciling",
    proof: "the completeness proof",
    review: "review",
    finalise: "finalising",
    notify: "sending the summary",
  };
  for (const s of STAGES) expect(stageText(s)).toBe(expected[s]);
});

test("every CellStatus has a non-empty string matching ui-copy.md §1", () => {
  const expected: Record<CellStatus, string> = {
    CLEAR: "clear",
    UNCLEAR: "unclear",
    CORRECTED: "corrected",
    ILLEGIBLE: "unreadable",
  };
  for (const s of CELL_STATUSES) expect(cellStatusLabel(s)).toBe(expected[s]);
});

test("every ReviewReason has a non-empty string matching ui-copy.md §5", () => {
  const expected: Record<ReviewReason, string> = {
    ILLEGIBLE: "unreadable on the register",
    CORRECTED: "corrected on the register",
    UNCLEAR: "unclear on the register",
    READS_DISAGREE: "the two reads disagree",
    ROW_IN_ONE_READ: "row seen in only one read",
    PAGE_LABEL_MISMATCH: "page number or month differs from the upload",
    UNKNOWN_SUPPLIER: "Supplier name not recognised",
    UNKNOWN_UNIT: "Unit not recognised",
    UNPARSEABLE_DATE: "date could not be read",
    UNMATCHED_NO_KEY: "row has no GRN and no single match",
    CONFLICTING_ROWS_IN_SOURCE: "two different rows in one record",
    SEQUENCE_GAP: "GRN number missing from every record",
    BACKDATED_ENTRY: "Entered late and back-dated",
    DATE_OUT_OF_PERIOD: "Date outside the month",
  };
  for (const r of REVIEW_REASONS) expect(reviewReasonLabel(r)).toBe(expected[r]);
});

test("every AuditAction has a non-empty string matching ui-copy.md §7", () => {
  const expected: Record<AuditAction, string> = {
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
  for (const a of AUDIT_ACTIONS) expect(auditActionLabel(a)).toBe(expected[a]);
});

// ui-copy.md has no table for ProblemCode (EP-08 validation problems are shown via the API's
// verbatim `message`, never composed in TS). problemCodeLabel is a best-effort fallback label for
// completeness/debugging only; this test checks completeness, not a ui-copy.md match (none exists).
test("every ProblemCode has a non-empty fallback string (no ui-copy.md table exists)", () => {
  for (const p of PROBLEM_CODES) expect(problemCodeLabel(p).length).toBeGreaterThan(0);
});

// ui-copy.md has no single table for ErrorCode either; only some codes have an anchored string
// elsewhere in ui-copy.md / spec.md (noted per-code in lib/wording.ts). This test pins the ones
// with a clear ui-copy.md/spec.md anchor and otherwise only checks completeness.
test("ErrorCode strings match their ui-copy.md/spec.md anchors where one exists", () => {
  expect(errorCodeLabel("NOT_ALLOWED")).toBe("Your email is not on the list for this app");
  expect(errorCodeLabel("NOT_FOUND")).toBe("Run not found");
  expect(errorCodeLabel("DUPLICATE_RUN")).toBe(
    "This exact month and files were already finalised",
  );
  expect(errorCodeLabel("PROOF_NOT_PASSED")).toBe("Proof has not passed");
  expect(errorCodeLabel("RUN_STATE_CONFLICT")).toBe("This run is finalised and read-only.");
  for (const e of ERROR_CODES) expect(errorCodeLabel(e).length).toBeGreaterThan(0);
});
