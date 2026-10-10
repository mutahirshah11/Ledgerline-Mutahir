"use client";

// 006-web T034: StepTracker (contracts/components.md; FR-011, FR-012, NFR-09). Ordered list of
// Upload → Validate → Read page 1…n → Reconcile; the current step carries aria-current="step";
// each step shows its state as text beside the icon, its status line, and, when failed, the
// error message and "Retry this step".

import { CheckCircle2, Circle, Loader2, RotateCcw, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { stepLines, type Step, type StepKey, type StepState } from "@/lib/run-steps";

export type StepTrackerProps = {
  steps: Step[];
  onRetry: (stepKey: Step["key"]) => void;
  /** CONTROLLER or OPS (F-12.R4). */
  canRetry: boolean;
  /** Extra content under a step (e.g. upload problems, "Open that run"). */
  extra?: Partial<Record<StepKey, ReactNode>>;
};

const STATE_TEXT: Record<StepState, string> = {
  pending: "Waiting",
  running: "Running",
  done: "Done",
  failed: "Failed",
};

const STATE_ICON: Record<StepState, ReactNode> = {
  pending: <Circle className="h-4 w-4" aria-hidden="true" />,
  running: <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />,
  done: <CheckCircle2 className="h-4 w-4" aria-hidden="true" />,
  failed: <XCircle className="h-4 w-4" aria-hidden="true" />,
};

const STATE_CLASS: Record<StepState, string> = {
  pending: "text-muted-foreground",
  running: "text-blue-700 dark:text-blue-300",
  done: "text-green-700 dark:text-green-400",
  failed: "text-red-700 dark:text-red-400",
};

/** The circular rail marker's own colours (background + border), independent of the text tone. */
const MARKER_CLASS: Record<StepState, string> = {
  pending: "border-border bg-muted text-muted-foreground",
  running: "border-blue-300 bg-blue-100 text-blue-700 dark:border-blue-700 dark:bg-blue-950/50 dark:text-blue-300",
  done: "border-green-300 bg-green-100 text-green-700 dark:border-green-800 dark:bg-green-950/50 dark:text-green-300",
  failed: "border-red-300 bg-red-100 text-red-700 dark:border-red-800 dark:bg-red-950/50 dark:text-red-300",
};

/** The step to mark current: the running or failed one, else the first waiting one. */
function currentKey(steps: Step[]): StepKey | null {
  const active = steps.find((s) => s.state === "running" || s.state === "failed");
  if (active) return active.key;
  return steps.find((s) => s.state === "pending")?.key ?? null;
}

export function StepTracker({ steps, onRetry, canRetry, extra }: StepTrackerProps) {
  const current = currentKey(steps);
  return (
    <ol aria-label="Steps" className="flex flex-col">
      {steps.map((s, i) => {
        const isCurrent = s.key === current;
        const isLast = i === steps.length - 1;
        return (
          <li key={s.key} aria-current={isCurrent ? "step" : undefined} className="relative flex gap-3">
            <div className="flex flex-col items-center">
              <span
                className={cn(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border-2",
                  MARKER_CLASS[s.state],
                )}
              >
                {STATE_ICON[s.state]}
              </span>
              {!isLast ? (
                <span
                  aria-hidden="true"
                  className={cn("w-px flex-1", s.state === "done" ? "bg-green-300 dark:bg-green-800" : "bg-border")}
                />
              ) : null}
            </div>
            <div
              className={cn(
                "mb-4 min-w-0 flex-1 rounded-lg border p-3",
                s.state === "failed" && "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/40",
                isCurrent && s.state !== "failed" && "border-blue-300 bg-blue-50/50 dark:border-blue-700 dark:bg-blue-950/20",
                !isCurrent && s.state !== "failed" && "border-border",
              )}
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className={cn("font-semibold", STATE_CLASS[s.state])}>{s.label}</span>
                <span className={cn("text-xs font-medium tracking-wide uppercase", STATE_CLASS[s.state])}>
                  {STATE_TEXT[s.state]}
                </span>
              </div>
              {s.line || s.attempt ? (
                <p className="mt-1 text-sm text-foreground">
                  {s.line}
                  {s.attempt && s.state === "running" ? (
                    <span className="ml-2 text-amber-800 dark:text-amber-300">{stepLines.retrying(s.attempt)}</span>
                  ) : null}
                </p>
              ) : null}
              {s.state === "failed" && s.error ? (
                <p className="mt-1 text-sm text-red-800 dark:text-red-300">{s.error.message}</p>
              ) : null}
              {extra?.[s.key]}
              {s.state === "failed" && s.retryable && canRetry ? (
                <Button type="button" variant="outline" size="sm" className="mt-2" onClick={() => onRetry(s.key)}>
                  <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                  Retry this step
                </Button>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
