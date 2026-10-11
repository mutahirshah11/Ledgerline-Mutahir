import { ArrowRight, BookOpen, ClipboardList, FileSpreadsheet, Flag } from "lucide-react";

import { sourceLabel, type Source } from "@/lib/wording";
import { formatMonthLabel } from "@/lib/time";
import type { components } from "@/lib/api-types";

type RunView = components["schemas"]["RunView"];

const SOURCE_ICON: Record<Source, typeof BookOpen> = {
  REGISTER: BookOpen,
  ERP: ClipboardList,
  SHEET: FileSpreadsheet,
};

const SOURCE_TONE: Record<Source, string> = {
  REGISTER: "bg-primary-soft text-primary",
  ERP: "bg-success-soft text-success-ink",
  SHEET: "bg-warning-soft text-warning-ink",
};

const SOURCES: Source[] = ["REGISTER", "ERP", "SHEET"];

export type SourceCheckCardProps = {
  run: RunView;
};

/** Sums every outcome count for one source: row_counts is {Source: {Outcome: count}} (data-model
 * §2) — summing every nested value, rather than reading named keys, is robust to the exact
 * Outcome set the API returns without this component having to enumerate it. */
function lineCountFor(run: RunView, source: Source): number {
  const bySource = run.row_counts[source];
  if (!bySource) return 0;
  return Object.values(bySource).reduce((sum, n) => sum + n, 0);
}

export function SourceCheckCard({ run }: SourceCheckCardProps) {
  const month = formatMonthLabel(run.month);
  // A FINALISED run is closed regardless of what `open_review_items` still reads (e.g. items
  // that didn't need a decision, like duplicates) — trust the finalised state over that count so
  // this card never contradicts the hero's "You're all caught up." / "closed" copy. It gets its
  // own branch below rather than folding into "All lines matched", since a finalised run may well
  // have had — and resolved — real mismatches; "matched" would misstate that.
  const finalised = run.status === "FINALISED";
  const needsAttention = finalised ? 0 : run.open_review_items;
  const totalLines = SOURCES.reduce((sum, s) => sum + lineCountFor(run, s), 0);
  const nothingReadYet = totalLines === 0;
  // Receipts (GRN groups) whose sources disagree or are missing: decided or not, they are not
  // "matched", so the card never says "All lines matched" over them.
  const exceptions =
    (run.counts.MISMATCHED ?? 0) +
    (run.counts.MISSING ?? 0) +
    (run.counts.NEEDS_REVIEW ?? 0) +
    (run.counts.UNMATCHED_NO_KEY ?? 0);

  return (
    <div className="rounded-2xl border border-border bg-card p-5 shadow-sm sm:p-6">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="font-heading text-lg font-bold tracking-tight text-foreground">How {month} was checked</h3>
        <p className="text-xs text-muted-foreground">Ledgerline reads all three, then compares every line</p>
      </div>
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-center">
        {SOURCES.map((source, i) => {
          const Icon = SOURCE_ICON[source];
          return (
            <div key={source} className="flex items-center gap-3 rounded-xl bg-muted/60 p-3">
              <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${SOURCE_TONE[source]}`}>
                <Icon className="h-4.5 w-4.5" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-foreground">{sourceLabel(source)}</p>
                <p className="text-xs text-muted-foreground">{lineCountFor(run, source)} lines</p>
              </div>
              {i < SOURCES.length - 1 ? (
                <ArrowRight className="ml-auto hidden h-4 w-4 shrink-0 text-faint sm:block" aria-hidden="true" />
              ) : null}
            </div>
          );
        })}
        <div
          className={`rounded-xl p-3 text-sm font-medium ${
            nothingReadYet
              ? "bg-muted text-muted-foreground"
              : needsAttention > 0
                ? "bg-warning-soft text-warning-ink"
                : "bg-success-soft text-success-ink"
          }`}
        >
          <p className="flex items-center gap-1.5">
            <Flag className="h-4 w-4 shrink-0" aria-hidden="true" />
            {nothingReadYet
              ? "Nothing checked yet"
              : needsAttention > 0
                ? `${run.counts.MATCHED ?? 0} matched, ${needsAttention} to check`
                : exceptions > 0
                  ? `${run.counts.MATCHED ?? 0} matched, ${exceptions} exception${exceptions === 1 ? "" : "s"}`
                  : "All lines matched"}
          </p>
          <p className="mt-0.5 text-xs opacity-80">
            {nothingReadYet
              ? "No lines have been read for this run"
              : needsAttention > 0
                ? "Waiting on your decision"
                : finalised
                  ? `Finalised · ${totalLines} rows placed once`
                  : exceptions > 0
                    ? "Reviewed, ready to finalise"
                    : "Nothing left to review"}
          </p>
        </div>
      </div>
    </div>
  );
}
