"use client";

// The real, contract-bound "every month at a glance" table (SCR-02, e2e/scr-02-runs.spec.ts).
// Visually it's the redesign's monthly-rows card; functionally every row keeps the exact
// behaviour/attributes the existing e2e suite pins: `data-testid="run-row-{id}"`, a real <table>
// (role=table), click-or-Enter navigates straight to /runs/{id}, a RunStatusBadge and one
// OutcomeBadge per non-zero outcome (same aria-labels as before), and the literal "Proof
// passed"/"Proof failed"/"Not run yet" text. The raw "YYYY-MM" month string stays in the DOM
// (sr-only) alongside the new human-readable month label, so nothing that reads it breaks.

import { CheckCircle2, ChevronRight, CircleSlash, XCircle } from "lucide-react";
import { useRouter } from "next/navigation";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import { formatMonthLabel, formatMonthShort, formatRelative } from "@/lib/time";
import { cn } from "@/lib/utils";
import type { RunSummary } from "@/components/RunsTable";
import type { Outcome } from "@/lib/wording";

function proofText(status: RunSummary["proof_status"]): string {
  if (status === "PASS") return "Proof passed";
  if (status === "FAIL") return "Proof failed";
  return "Not run yet";
}

function nonZeroOutcomes(counts: RunSummary["counts"]): { outcome: Outcome; count: number }[] {
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([outcome, count]) => ({ outcome: outcome as Outcome, count }));
}

function rowTone(run: RunSummary): { tile: string; label: string; text: string } {
  if (run.status === "FAILED") return { tile: "bg-danger-soft text-danger-ink", label: "Didn't finish", text: "text-danger-ink" };
  const total = Object.values(run.counts).reduce((s, n) => s + n, 0);
  const matched = (run.counts.MATCHED ?? 0) + (run.counts.MATCHED_AFTER_CONVERSION ?? 0);
  if (run.status === "FINALISED" && total > 0 && matched === total) {
    return { tile: "bg-success-soft text-success-ink", label: "All matched", text: "text-success-ink" };
  }
  if (total - matched > 0) return { tile: "bg-warning-soft text-warning-ink", label: "Needs you", text: "text-warning-ink" };
  return { tile: "bg-muted text-muted-foreground", label: "In progress", text: "text-muted-foreground" };
}

function outcomeSummary(run: RunSummary): string {
  const total = Object.values(run.counts).reduce((s, n) => s + n, 0);
  if (total === 0) return "No lines yet.";
  const matched = (run.counts.MATCHED ?? 0) + (run.counts.MATCHED_AFTER_CONVERSION ?? 0);
  const outstanding = total - matched;
  if (outstanding === 0) return `All ${total} lines reconciled`;
  return `${matched} matched · ${outstanding} need${outstanding === 1 ? "s" : ""} a decision`;
}

function segments(run: RunSummary): { success: number; warning: number; danger: number } {
  const total = Object.values(run.counts).reduce((s, n) => s + n, 0) || 1;
  const matched = (run.counts.MATCHED ?? 0) + (run.counts.MATCHED_AFTER_CONVERSION ?? 0);
  if (run.status === "FAILED") return { success: 0, warning: 0, danger: 100 };
  const outstanding = total - matched;
  return { success: (matched / total) * 100, warning: (outstanding / total) * 100, danger: 0 };
}

export type MonthlyTableProps = {
  runs: RunSummary[];
};

export function MonthlyTable({ runs }: MonthlyTableProps) {
  const router = useRouter();

  return (
    <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider px-5 py-4">
        <h3 className="font-heading text-base font-bold tracking-tight text-foreground">Every month at a glance</h3>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-[2px] bg-success" /> Matched
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-[2px] bg-warning" /> Needs you
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-[2px] bg-danger" /> Couldn&apos;t read
          </span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader className="sr-only">
            <TableRow>
              <TableHead>Month</TableHead>
              <TableHead>Status and outcomes</TableHead>
              <TableHead>Progress</TableHead>
              <TableHead>Updated</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => {
              const tone = rowTone(run);
              const bar = segments(run);
              const href = `/runs/${run.id}`;
              const open = () => router.push(href);
              const ProofIcon =
                run.proof_status === "PASS" ? CheckCircle2 : run.proof_status === "FAIL" ? XCircle : CircleSlash;
              return (
                <TableRow
                  key={run.id}
                  data-testid={`run-row-${run.id}`}
                  tabIndex={0}
                  aria-label={`Open run ${run.month}`}
                  className="cursor-pointer border-divider transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-inset"
                  onClick={open}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      open();
                    }
                  }}
                >
                  <TableCell className="w-56 py-4">
                    <div className="flex items-center gap-3">
                      <span className={cn("flex h-[46px] w-[46px] shrink-0 flex-col items-center justify-center rounded-xl text-[10px] font-bold", tone.tile)}>
                        {formatMonthShort(run.month)}
                      </span>
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-foreground">
                          {formatMonthLabel(run.month)}
                          <span className="sr-only"> ({run.month})</span>
                        </p>
                        <p className={cn("text-xs font-medium", tone.text)}>{tone.label}</p>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell className="py-4">
                    <p className="text-sm text-foreground">{outcomeSummary(run)}</p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <RunStatusBadge status={run.status} className="h-5 px-1.5 text-[10px]" />
                      {nonZeroOutcomes(run.counts).map(({ outcome, count }) => (
                        <OutcomeBadge key={outcome} outcome={outcome} count={count} className="h-5 px-1.5 text-[10px]" />
                      ))}
                      <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                        <ProofIcon className="h-3 w-3 shrink-0" aria-hidden="true" />
                        {proofText(run.proof_status)}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="w-40 py-4">
                    <div className="flex h-2 w-full overflow-hidden rounded-full bg-divider">
                      {bar.success > 0 && <div className="bg-success" style={{ width: `${bar.success}%` }} />}
                      {bar.warning > 0 && (
                        <div className="bg-warning" style={{ width: `${Math.max(bar.warning, 6)}%` }} />
                      )}
                      {bar.danger > 0 && <div className="bg-danger" style={{ width: `${bar.danger}%` }} />}
                    </div>
                  </TableCell>
                  <TableCell className="w-32 py-4 text-right">
                    <div className="flex items-center justify-end gap-1.5 text-xs text-muted-foreground">
                      <time dateTime={run.created_at}>{formatRelative(run.created_at)}</time>
                      <ChevronRight className="h-4 w-4 shrink-0" aria-hidden="true" />
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
