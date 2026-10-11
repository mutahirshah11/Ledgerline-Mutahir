import { expect, test } from "@playwright/test";

import {
  initialSteps,
  latestRetryAttempt,
  planSteps,
  previousMonth,
  readCostActualLine,
  readCostEstimateLine,
  stepLines,
  type PlanInput,
  type Step,
} from "../../lib/run-steps";
import type { RunStatus, Stage } from "../../lib/wording";

// 006-web T030: lib/run-steps.ts, the SCR-03 step planner (data-model.md §3.1, research R8/R9,
// FR-011, FR-014) and the step/read-cost lines (contracts/ui-copy.md §3 and "Read cost").

type Pages = PlanInput["stage_progress"]["pages"];

const TWO_PAGES_NONE_READ: Pages = [
  { page_no: 1, extracted: false, cached: false },
  { page_no: 2, extracted: false, cached: false },
];
const PAGE_1_READ: Pages = [
  { page_no: 1, extracted: true, cached: true },
  { page_no: 2, extracted: false, cached: false },
];
const BOTH_READ: Pages = [
  { page_no: 1, extracted: true, cached: true },
  { page_no: 2, extracted: true, cached: false },
];

function run(
  status: RunStatus,
  pages: Pages,
  failed: { stage: Stage; message: string } | null = null,
): PlanInput {
  return {
    status,
    failed_stage: failed?.stage ?? null,
    error_message: failed?.message ?? null,
    stage_progress: { pages },
  };
}

const states = (steps: Step[]) => Object.fromEntries(steps.map((s) => [s.key, s.state]));

test("initialSteps: Upload, Validate, Read page 1..n, Reconcile, all waiting", () => {
  const steps = initialSteps(2);
  expect(steps.map((s) => [s.key, s.label, s.state])).toEqual([
    ["upload", "Upload", "pending"],
    ["validate", "Validate", "pending"],
    ["page:1", "Read page 1", "pending"],
    ["page:2", "Read page 2", "pending"],
    ["reconcile", "Reconcile", "pending"],
  ]);
  expect(steps.every((s) => s.line === null && s.error === null && s.attempt === null)).toBe(true);
});

test("§3.1 CREATED: nothing done, next upload (pages from the drop)", () => {
  const plan = planSteps(run("CREATED", []), 2);
  expect(plan.next).toBe("upload");
  expect(states(plan.steps)).toEqual({
    upload: "pending",
    validate: "pending",
    "page:1": "pending",
    "page:2": "pending",
    reconcile: "pending",
  });
});

test("§3.1 UPLOADED: upload done, next validate", () => {
  const plan = planSteps(run("UPLOADED", PAGE_1_READ));
  expect(plan.next).toBe("validate");
  expect(states(plan.steps)).toMatchObject({ upload: "done", validate: "pending", "page:1": "pending" });
});

for (const status of ["VALIDATED", "EXTRACTING"] as const) {
  test(`§3.1 ${status}: upload, validate and read pages done, next first unread page`, () => {
    const none = planSteps(run(status, TWO_PAGES_NONE_READ));
    expect(none.next).toBe("page:1");
    expect(states(none.steps)).toMatchObject({ upload: "done", validate: "done", "page:1": "pending" });

    const plan = planSteps(run(status, PAGE_1_READ));
    expect(plan.next).toBe("page:2");
    expect(states(plan.steps)).toEqual({
      upload: "done",
      validate: "done",
      "page:1": "done",
      "page:2": "pending",
      reconcile: "pending",
    });
  });
}

test("§3.1 EXTRACTED: all pages done, next reconcile", () => {
  const plan = planSteps(run("EXTRACTED", BOTH_READ));
  expect(plan.next).toBe("reconcile");
  expect(states(plan.steps)).toMatchObject({ "page:1": "done", "page:2": "done", reconcile: "pending" });
});

for (const status of ["RECONCILED", "AWAITING_REVIEW", "READY", "FINALISED"] as const) {
  test(`§3.1 ${status}: everything done, go to the run overview`, () => {
    const plan = planSteps(run(status, BOTH_READ));
    expect(plan.next).toBe("overview");
    expect(plan.steps.every((s) => s.state === "done")).toBe(true);
  });
}

test("§3.1 FAILED at extract: earlier steps done, first unread page failed with the message", () => {
  const msg = "Page 2: openai failed: timeout after 3 attempts";
  const plan = planSteps(run("FAILED", PAGE_1_READ, { stage: "extract", message: msg }));
  expect(plan.next).toBe("page:2");
  expect(states(plan.steps)).toEqual({
    upload: "done",
    validate: "done",
    "page:1": "done",
    "page:2": "failed",
    reconcile: "pending",
  });
  const failed = plan.steps.find((s) => s.key === "page:2")!;
  expect(failed.error).toEqual({ message: msg, code: null });
  expect(failed.retryable).toBe(true);
});

test("§3.1 FAILED at extract with no page read: page 1 failed", () => {
  const plan = planSteps(run("FAILED", TWO_PAGES_NONE_READ, { stage: "extract", message: "x" }));
  expect(plan.next).toBe("page:1");
  expect(states(plan.steps)).toMatchObject({ "page:1": "failed", "page:2": "pending" });
});

for (const stage of ["reconcile", "proof"] as const) {
  test(`§3.1 FAILED at ${stage}: all pages done, reconcile failed and retryable`, () => {
    const plan = planSteps(run("FAILED", BOTH_READ, { stage, message: "C1 failed" }));
    expect(plan.next).toBe("reconcile");
    expect(states(plan.steps)).toMatchObject({ "page:2": "done", reconcile: "failed" });
    expect(plan.steps.at(-1)!.retryable).toBe(true);
  });
}

test("§3.1 FAILED at validate: upload done, validate failed, not retryable (FR-024)", () => {
  const msg = "1 problem: Store sheet: no worksheet named Aug-2026 (found: Sep-2026)";
  const plan = planSteps(run("FAILED", BOTH_READ, { stage: "validate", message: msg }));
  expect(plan.next).toBe("validate");
  expect(states(plan.steps)).toEqual({
    upload: "done",
    validate: "failed",
    "page:1": "pending",
    "page:2": "pending",
    reconcile: "pending",
  });
  const v = plan.steps.find((s) => s.key === "validate")!;
  expect(v.error?.message).toBe(msg);
  expect(v.retryable).toBe(false);
});

test("§3.1 FAILED at upload: upload failed, not retryable (create the run again)", () => {
  const plan = planSteps(run("FAILED", [], { stage: "upload", message: "files changed" }), 2);
  expect(plan.next).toBe("upload");
  expect(states(plan.steps)).toMatchObject({ upload: "failed", validate: "pending" });
  expect(plan.steps[0].retryable).toBe(false);
});

test("ui-copy §3 step lines", () => {
  expect(stepLines.uploading(4)).toBe("Uploading 4 files");
  expect(stepLines.uploaded(2, 0)).toBe("2 new pages to read");
  expect(stepLines.uploaded(1, 1)).toBe("1 new page to read · 1 seen in an earlier run, reused (no AI call)");
  expect(stepLines.reading(2, 2)).toBe("Reading page 2 of 2…");
  expect(stepLines.readDone(2, 2, { cached: true, rows: 14, review_cells: 0 })).toBe(
    "Page 2 of 2: same photo as an earlier run, its AI read reused (14 rows, 0 cells need you)",
  );
  expect(stepLines.readDone(2, 2, { cached: false, rows: 14, review_cells: 2 })).toBe(
    "Reading page 2 of 2… done (new read, 14 rows, 2 cells need you)",
  );
  expect(stepLines.retrying(2)).toBe("retrying (attempt 2 of 3)");
  expect(stepLines.reconciled).toBe("Reconciled");
  expect(stepLines.timeout).toBe("Timed out. Try this step again.");
});

test("read cost lines: estimate, all cached, actual, prices unset", () => {
  expect(readCostEstimateLine(2, "0.023")).toBe("2 new pages · est. US$0.023");
  expect(readCostEstimateLine(1, "0.012")).toBe("1 new page · est. US$0.012");
  expect(readCostEstimateLine(0, "0.000")).toBe("US$0 · same photos as an earlier run, reads reused");
  expect(readCostEstimateLine(2, "0.000")).toBe("US$0 · same photos as an earlier run, reads reused");
  expect(readCostEstimateLine(0, "0.012")).toBe("est. US$0.012");
  expect(readCostEstimateLine(2, null)).toBe("Read cost not available");
  expect(readCostActualLine("0.022")).toBe("Read cost US$0.022");
  expect(readCostActualLine(null)).toBe("Read cost not available");
});

test("EP-19: latest event for the step; RETRY gives its attempt, anything else none", () => {
  const ev = (stage: Stage, status: string, detail: Record<string, unknown>) => ({ stage, status, detail });
  const events = [
    ev("extract", "RETRY", { attempt: 2, page_no: 2, variant: "A" }),
    ev("extract", "STARTED", { page_no: 2 }),
    ev("extract", "OK", { page_no: 1, cached: false }),
    ev("extract", "RETRY", { attempt: 3, page_no: 1 }),
  ];
  expect(latestRetryAttempt(events, "page:2")).toBe(2);
  expect(latestRetryAttempt(events, "page:1")).toBeNull();
  expect(latestRetryAttempt(events, "reconcile")).toBeNull();
  expect(latestRetryAttempt([ev("reconcile", "RETRY", { attempt: 3 })], "reconcile")).toBe(3);
});

test("previousMonth: the month before the scenario clock (EP-01 scenario_now, its own offset)", () => {
  expect(previousMonth("2026-10-15T08:30:00+05:00")).toBe("2026-09");
  expect(previousMonth("2026-01-01T00:10:00+05:00")).toBe("2025-12");
  expect(previousMonth("2026-12-31T23:59:00Z")).toBe("2026-11");
});
