"use client";

// 006-web T034: StepTracker (contracts/components.md; FR-011, FR-012, NFR-09). Ordered list of
// Upload → Validate → Read page 1…n → Reconcile; the current step carries aria-current="step";
// each step shows its state as text beside the icon, its status line, and, when failed, the
// error message and "Retry this step".

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

const STATE_ICON: Record<StepState, string> = { pending: "○", running: "◐", done: "✓", failed: "✕" };

const STATE_CLASS: Record<StepState, string> = {
  pending: "text-muted-foreground",
  running: "text-blue-700 dark:text-blue-300",
  done: "text-green-700 dark:text-green-400",
  failed: "text-red-700 dark:text-red-400",
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
    <ol aria-label="Steps" className="flex flex-col gap-2">
      {steps.map((s) => (
        <li
          key={s.key}
          aria-current={s.key === current ? "step" : undefined}
          className={cn(
            "rounded-md border p-3",
            s.state === "failed" && "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/40",
            s.key === current && s.state !== "failed" && "border-blue-300",
          )}
        >
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className={cn("font-medium", STATE_CLASS[s.state])}>
              <span aria-hidden="true">{STATE_ICON[s.state]} </span>
              <span>{s.label}</span>
            </span>
            <span className={cn("text-sm", STATE_CLASS[s.state])}>{STATE_TEXT[s.state]}</span>
          </div>
          {s.line || s.attempt ? (
            <p className="mt-1 text-sm">
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
              Retry this step
            </Button>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
