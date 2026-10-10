"use client";

// 006-web T035/T036: SCR-03 "Start a run" (spec.md US2, SCR-03-AC1..AC12, FR-008..FR-015;
// contracts/screens.md SCR-03; research R6..R9; data-model.md §3.1 and "Read cost").
//
// Run = EP-03 → SHA-256 of each file → EP-06 → PUT each file to its signed link (at most 4 at
// once) → EP-07 → EP-08 → EP-09 once per page in page order → EP-10 → /runs/{id}. One request
// per step; a failed step stops the sequence and offers "Retry this step", which re-calls only
// that step (for an upload FM-01/FM-02 failure: new links for the named files only). `?run={id}`
// resumes from EP-05 via planSteps(). While a step is in flight EP-19 is polled every 2 s to show
// "retrying (attempt n of 3)". Money and counts are shown exactly as the API sends them.

import { useQuery } from "@tanstack/react-query";
import { PlayCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useMe } from "@/app/providers";
import { FileDropZone } from "@/components/FileDropZone";
import { ReadCost, type ReadCostProps } from "@/components/ReadCost";
import { ErrorState, Loading } from "@/components/states";
import { StepTracker } from "@/components/StepTracker";
import { Button } from "@/components/ui/button";
import { ApiError, api, isUnreachable, type ApiResponse } from "@/lib/api";
import type { components } from "@/lib/api-types";
import { checkDrop } from "@/lib/file-rules";
import { sha256Hex } from "@/lib/hash";
import {
  initialSteps,
  latestRetryAttempt,
  pageOf,
  planSteps,
  previousMonth,
  stepLines,
  type Step,
  type StepError,
  type StepKey,
} from "@/lib/run-steps";
import type { Source } from "@/lib/wording";

type RunView = ApiResponse<"get", "/runs/{id}">;
type Problem = components["schemas"]["ValidationProblem"];
type Upload = { file: File; source: Source; pageNo: number | null };
type Drop = { register: File[]; erp: File[]; sheet: File[] };

const UNREACHABLE = "Ledgerline is not reachable right now";
/** A step request longer than the platform's 300 s limit has failed (FM-13). */
const STEP_TIMEOUT_MS = 310_000;
const MAX_PARALLEL_UPLOADS = 4;
const POLL_MS = 2_000;
const SOURCES: Source[] = ["REGISTER", "ERP", "SHEET"];
/** ui-copy.md §3 "validate done": the zone names. */
const ZONE_LABEL: Record<Source, string> = { REGISTER: "Register pages", ERP: "ERP", SHEET: "Store sheet" };

// --- helpers ------------------------------------------------------------------------------------

function stepError(err: unknown): StepError {
  if (err instanceof DOMException && err.name === "AbortError") return { message: stepLines.timeout, code: "TIMEOUT" };
  if (err instanceof ApiError) {
    if (err.status === 504 || err.status === 408) return { message: stepLines.timeout, code: err.code };
    if (isUnreachable(err)) return { message: UNREACHABLE, code: err.code };
    return { message: err.message, code: err.code };
  }
  return { message: err instanceof Error ? err.message : String(err), code: null };
}

/** Runs `fn` with an abort signal that fires after STEP_TIMEOUT_MS. */
async function withTimeout<T>(fn: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), STEP_TIMEOUT_MS);
  try {
    return await fn(ctl.signal);
  } finally {
    clearTimeout(timer);
  }
}

async function inParallel<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (next < items.length) await fn(items[next++]);
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}

/** research R7: plain PUT of the file to its signed link (the browser holds no storage key). */
async function putFile(url: string, file: File): Promise<void> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: "PUT",
      body: file,
      headers: { "content-type": file.type || "application/octet-stream" },
    });
  } catch {
    throw new ApiError({ status: 0, code: "NETWORK_ERROR", message: "Network request failed" });
  }
  if (!res.ok) {
    throw new ApiError({ status: res.status, code: "BAD_RESPONSE", message: `Upload of ${file.name} failed` });
  }
}

function uploadsOf(drop: Drop): Upload[] {
  return [
    ...drop.register.map((file, i) => ({ file, source: "REGISTER" as const, pageNo: i + 1 })),
    ...drop.erp.map((file) => ({ file, source: "ERP" as const, pageNo: null })),
    ...drop.sheet.map((file) => ({ file, source: "SHEET" as const, pageNo: null })),
  ];
}

function readingStarted(run: RunView): boolean {
  return (
    run.stage_progress.pages.some((p) => p.extracted) ||
    run.failed_stage === "extract" ||
    ["EXTRACTED", "RECONCILED", "AWAITING_REVIEW", "READY", "FINALISED"].includes(run.status)
  );
}

const move = <T,>(list: T[], from: number, to: number): T[] => {
  const out = [...list];
  const [item] = out.splice(from, 1);
  out.splice(to, 0, item);
  return out;
};

// --- screen -------------------------------------------------------------------------------------

export function NewRun({ resumeId }: { resumeId: string | null }) {
  const router = useRouter();
  const me = useMe();

  const [month, setMonth] = useState("");
  const [drop, setDrop] = useState<Drop>({ register: [], erp: [], sheet: [] });
  const [steps, setStepsState] = useState<Step[] | null>(null);
  const [runId, setRunId] = useState<string | null>(resumeId);
  const [runningKey, setRunningKey] = useState<StepKey | null>(null);
  const [checks, setChecks] = useState<Problem[] | null>(null);
  const [duplicateOf, setDuplicateOf] = useState<string | null>(null);
  const [estimate, setEstimate] = useState<ReadCostProps["estimate"]>(null);
  const [actualUsd, setActualUsd] = useState<string | null | undefined>(undefined);
  const [resumeState, setResumeState] = useState<"none" | "loading" | "ready" | "created">(
    resumeId ? "loading" : "none",
  );
  const [resumeError, setResumeError] = useState<ApiError | null>(null);

  const stepsRef = useRef<Step[] | null>(null);
  const runIdRef = useRef<string | null>(resumeId);
  const dropRef = useRef<Drop>(drop);
  const monthRef = useRef(month);
  const retryUploadsRef = useRef<Upload[] | null>(null);
  const pageCountRef = useRef(0);

  const setSteps = (next: Step[]) => {
    stepsRef.current = next;
    setStepsState(next);
  };
  const patch = (key: StepKey, change: Partial<Step>) => {
    const current = stepsRef.current;
    if (current) setSteps(current.map((s) => (s.key === key ? { ...s, ...change } : s)));
  };

  // EP-01: default month = the month before the scenario clock's month (SCR-03-AC1).
  useEffect(() => {
    let live = true;
    api
      .get("/health")
      .then((h) => {
        if (live) setMonth((m) => m || previousMonth(h.scenario_now));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  // EP-19 while a step is in flight (research R9): first poll after 2 s, then every 2 s.
  const events = useQuery({
    queryKey: ["run-events", runId, runningKey],
    queryFn: ({ signal }) => api.get("/runs/{id}/events", { path: { id: runId! }, signal }),
    enabled: !!runId && !!runningKey,
    initialData: [],
    staleTime: Infinity,
    refetchInterval: POLL_MS,
    retry: false,
  });

  // --- steps ----------------------------------------------------------------------------------

  async function refreshCost(id: string) {
    try {
      const view = await api.get("/runs/{id}", { path: { id } });
      setActualUsd(view.read_cost.actual_usd ?? null);
    } catch {
      // the cost line is informative only; a failed read leaves it as it was
    }
  }

  async function doUpload(): Promise<boolean> {
    let id = runIdRef.current;
    if (!id) {
      const created = await withTimeout((signal) => api.post("/runs", { body: { month: monthRef.current }, signal }));
      id = created.id;
      runIdRef.current = id;
      setRunId(id);
    }
    const runFor = id;
    const all = uploadsOf(dropRef.current);
    const todo = retryUploadsRef.current ?? all;
    patch("upload", { line: stepLines.uploading(todo.length) });

    const files = await Promise.all(
      todo.map(async (u) => ({
        source: u.source,
        filename: u.file.name,
        size_bytes: u.file.size,
        sha256: await sha256Hex(u.file),
        page_no: u.pageNo,
      })),
    );
    const { uploads } = await withTimeout((signal) =>
      api.post("/runs/{id}/uploads", { path: { id: runFor }, body: { files }, signal }),
    );
    const pairs = todo.map((u) => {
      const slot =
        uploads.find((s) => s.source === u.source && s.filename === u.file.name && (s.page_no ?? null) === u.pageNo) ??
        uploads.find((s) => s.source === u.source && s.filename === u.file.name);
      if (!slot) throw new Error(`No upload link for ${u.file.name}`);
      return { u, url: slot.signed_url };
    });
    await inParallel(pairs, MAX_PARALLEL_UPLOADS, ({ u, url }) => putFile(url, u.file));

    try {
      const confirmed = await withTimeout((signal) =>
        api.post("/runs/{id}/uploads/confirm", { path: { id: runFor }, signal }),
      );
      retryUploadsRef.current = null;
      patch("upload", {
        state: "done",
        line: stepLines.uploaded(confirmed.new_pages.length, confirmed.known_pages.length),
      });
      setEstimate({ newPages: confirmed.new_pages.length, usd: confirmed.read_cost_estimate_usd ?? null });
      return true;
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.code === "DUPLICATE_RUN") {
        setDuplicateOf(typeof err.details.run_id === "string" ? err.details.run_id : null);
        patch("upload", { state: "failed", error: { message: stepLines.duplicate, code: err.code }, retryable: false });
        return false;
      }
      // FM-01 / FM-02: the next "Retry this step" asks for new links for the named files only.
      const named = err.details.files;
      if (err.status === 422 && Array.isArray(named)) {
        const bad = named as { filename?: string; source?: string }[];
        const again = all.filter((u) => bad.some((f) => f.filename === u.file.name && f.source === u.source));
        retryUploadsRef.current = again.length ? again : null;
        const names = again.map((u) => u.file.name).join(", ");
        const message =
          err.details.problem === "HASH_MISMATCH" && names ? `${stepLines.fileChanged}: ${names}` : err.message;
        patch("upload", { state: "failed", error: { message, code: err.code }, retryable: true });
        return false;
      }
      const missing = err.details.missing;
      if (err.code === "MISSING_SOURCE" && Array.isArray(missing)) {
        const again = all.filter((u) => (missing as string[]).includes(u.source));
        retryUploadsRef.current = again.length ? again : null;
      }
      throw err;
    }
  }

  async function doValidate(id: string): Promise<boolean> {
    try {
      const res = await withTimeout((signal) => api.post("/runs/{id}/validate", { path: { id }, signal }));
      setChecks(res.problems);
      patch("validate", { state: "done", line: null });
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 422 && Array.isArray(err.details.problems)) {
        const problems = err.details.problems as Problem[];
        setChecks(problems);
        const message = problems.length === 1 ? "1 problem in the files" : `${problems.length} problems in the files`;
        // validate has no retry from FAILED (FR-024): the files must be fixed in a new run.
        patch("validate", { state: "failed", error: { message, code: err.code }, retryable: false });
        return false;
      }
      throw err;
    }
  }

  async function doPage(id: string, n: number): Promise<boolean> {
    const total = pageCountRef.current;
    patch(`page:${n}`, { line: stepLines.reading(n, total) });
    const r = await withTimeout((signal) =>
      api.post("/runs/{id}/pages/{page_no}/extract", { path: { id, page_no: n }, signal }),
    );
    patch(`page:${n}`, { state: "done", line: stepLines.readDone(n, total, r) });
    return true;
  }

  async function doReconcile(id: string): Promise<boolean> {
    await withTimeout((signal) => api.post("/runs/{id}/reconcile", { path: { id }, signal }));
    patch("reconcile", { state: "done", line: stepLines.reconciled });
    return true;
  }

  async function runStep(key: StepKey): Promise<boolean> {
    if (key === "upload") return doUpload();
    const id = runIdRef.current!;
    if (key === "validate") return doValidate(id);
    if (key === "reconcile") return doReconcile(id);
    return doPage(id, pageOf(key)!);
  }

  /** Runs the steps from `start` in order until one fails or reconcile is done. */
  async function runFrom(start: StepKey) {
    const keys = (stepsRef.current ?? []).map((s) => s.key);
    for (let i = keys.indexOf(start); i >= 0 && i < keys.length; i++) {
      const key = keys[i];
      patch(key, { state: "running", error: null, attempt: null, retryable: true });
      setRunningKey(key);
      let ok = false;
      try {
        ok = await runStep(key);
      } catch (err) {
        patch(key, { state: "failed", error: stepError(err) });
      }
      setRunningKey(null);
      const page = pageOf(key);
      const lastPage = page !== null && page === pageCountRef.current;
      if (runIdRef.current && page !== null && (!ok || lastPage)) void refreshCost(runIdRef.current);
      if (!ok) return;
    }
    router.push(`/runs/${runIdRef.current}`);
  }

  // --- resume (?run={id}, SCR-03-AC11, FR-014) -------------------------------------------------

  const resumed = useRef(false);
  useEffect(() => {
    if (!resumeId || resumed.current) return;
    resumed.current = true;
    api
      .get("/runs/{id}", { path: { id: resumeId } })
      .then((run) => {
        const plan = planSteps(run);
        setMonth(run.month);
        monthRef.current = run.month;
        setEstimate({ newPages: run.read_cost.new_pages, usd: run.read_cost.estimate_usd ?? null });
        if (readingStarted(run)) setActualUsd(run.read_cost.actual_usd ?? null);
        if (plan.next === "overview") {
          router.replace(`/runs/${run.id}`);
          return;
        }
        if (run.status === "CREATED") {
          // Confirmed nothing yet: the files must be added again, then Run continues this run.
          setResumeState("created");
          return;
        }
        setResumeState("ready");
        pageCountRef.current = run.stage_progress.pages.length;
        setSteps(plan.steps);
        const next = plan.steps.find((s) => s.key === plan.next);
        if (next && next.state !== "failed") void runFrom(plan.next as StepKey);
      })
      .catch((err: unknown) => {
        setResumeError(err instanceof ApiError ? err : new ApiError({ status: 0, code: "NETWORK_ERROR", message: UNREACHABLE }));
      });
    // runFrom/router are stable for this screen's purpose; resume runs once per page load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeId]);

  // --- render ---------------------------------------------------------------------------------

  const check = checkDrop(drop);
  const started = steps !== null;
  const busy = runningKey !== null;
  const canRun = me.can.createRun && check.canRun && /^\d{4}-\d{2}$/.test(month) && !started;

  const onRun = () => {
    dropRef.current = drop;
    monthRef.current = month;
    pageCountRef.current = drop.register.length;
    setDuplicateOf(null);
    setChecks(null);
    setSteps(initialSteps(drop.register.length));
    void runFrom("upload");
  };

  const shownSteps = steps?.map((s) =>
    s.key === runningKey && s.state === "running" ? { ...s, attempt: latestRetryAttempt(events.data, s.key) } : s,
  );

  const zone = (source: Source, name: keyof Drop, accept: string, multiple: boolean) => {
    const z = check[name];
    return (
      <FileDropZone
        label={ZONE_LABEL[source]}
        name={name}
        accept={accept}
        multiple={multiple}
        files={drop[name]}
        problems={z.files}
        zoneProblem={z.zone}
        disabled={started}
        onFiles={(files) => setDrop((d) => ({ ...d, [name]: [...d[name], ...files] }))}
        onRemove={(i) => setDrop((d) => ({ ...d, [name]: d[name].filter((_, j) => j !== i) }))}
        onReorder={
          multiple ? (from, to) => setDrop((d) => ({ ...d, [name]: move(d[name], from, to) })) : undefined
        }
      />
    );
  };

  if (resumeError) {
    return (
      <div className="mx-auto w-full max-w-3xl p-6">
        <h1 className="mb-4 text-2xl font-semibold tracking-tight text-foreground">Start a run</h1>
        <ErrorState error={resumeError} />
      </div>
    );
  }

  const showForm = resumeState === "none" || resumeState === "created";

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">Start a run</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Add this month&apos;s register pages, ERP export and store sheet; Ledgerline reads, reconciles and proves them.
        </p>
      </div>
      {resumeState === "loading" ? <Loading /> : null}

      {showForm ? (
        <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-sm">
          <label className="flex flex-col gap-1 text-sm font-medium sm:max-w-xs">
            Month
            <input
              type="month"
              value={month}
              onChange={(e) => setMonth(e.target.value)}
              disabled={started || resumeState === "created"}
              className="rounded-md border border-border px-2.5 py-1.5 font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            />
          </label>
          <div className="grid gap-3 md:grid-cols-3">
            {zone("REGISTER", "register", "image/jpeg,image/png,.jpg,.jpeg,.png", true)}
            {zone("ERP", "erp", ".csv,text/csv", false)}
            {zone("SHEET", "sheet", ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", false)}
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
            <Button
              type="button"
              onClick={onRun}
              disabled={!canRun || busy}
              className="bg-emerald-700 hover:bg-emerald-700/90"
            >
              <PlayCircle className="h-4 w-4" aria-hidden="true" />
              Run
            </Button>
            {me.status === "ready" && !me.can.createRun ? (
              <p className="text-sm text-muted-foreground">Your role cannot do this</p>
            ) : null}
          </div>
        </div>
      ) : null}

      <ReadCost estimate={estimate} actualUsd={actualUsd} />

      {shownSteps ? (
        <div className="rounded-xl border border-border bg-card p-5 shadow-sm">
          <StepTracker
            steps={shownSteps}
            canRetry={me.can.retry}
            onRetry={(key) => {
              if (!busy) void runFrom(key);
            }}
            extra={{
              upload: duplicateOf ? (
                <Link href={`/runs/${duplicateOf}`} className="mt-1 inline-block text-sm text-emerald-700 underline dark:text-emerald-400">
                  {stepLines.openThatRun}
                </Link>
              ) : null,
              validate: checks ? (
                <ul aria-label="File checks" className="mt-2 flex flex-col gap-1 text-sm">
                  {SOURCES.map((source) => {
                    const mine = checks.filter((p) => p.source === source);
                    return mine.length === 0 ? (
                      <li key={source} className="text-green-800 dark:text-green-400">
                        <span aria-hidden="true">✓</span> {ZONE_LABEL[source]}: OK
                      </li>
                    ) : (
                      <li key={source} className="text-red-800 dark:text-red-300">
                        <span className="font-medium">
                          <span aria-hidden="true">✕</span> {ZONE_LABEL[source]}
                        </span>
                        {mine.map((p, i) => (
                          <p key={i}>{p.message}</p>
                        ))}
                      </li>
                    );
                  })}
                </ul>
              ) : null,
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
