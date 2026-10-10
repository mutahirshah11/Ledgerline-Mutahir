import { AlertTriangle } from "lucide-react";

import { cn } from "@/lib/utils";

// contracts/components.md "DiffCell" (006-web T053; US5, SCR-05-AC1/AC2). Shows a cell's recorded
// value and, beneath it, the compared (normalised) value only when they differ as strings. Differing
// cells are highlighted; the outlier source is marked with visible text (NFR-09: never colour alone).

export type DiffCellProps = {
  /** The raw recorded value from this source, e.g. "1200 YD". */
  recorded: string | null;
  /** The normalised/compared value, e.g. "1097.28 M". Shown beneath `recorded` only when both
   * exist and differ as strings. */
  compared?: string | null;
  /** Whether this field differs across sources. */
  differs: boolean;
  /** Whether this source is the outlier. Adds visible text "(different)" marker. */
  isOutlier: boolean;
  /** True when the source has no row for this group (renders "Not in {sourceLabel}"). */
  absent?: boolean;
  /** For screen readers: "Register", "ERP", or "Store sheet". */
  sourceLabel: string;
};

export function DiffCell({
  recorded,
  compared,
  differs,
  isOutlier,
  absent,
  sourceLabel,
}: DiffCellProps) {
  if (absent) {
    return (
      <span className="italic text-muted-foreground">
        Not in {sourceLabel}
      </span>
    );
  }

  const showCompared = compared != null && recorded != null && compared !== recorded;

  return (
    <span
      className={cn(
        "inline-flex flex-col gap-0.5 rounded-md border border-transparent px-1.5 py-1 transition-colors",
        differs && !isOutlier && "border-muted-foreground/15 bg-muted/40",
        isOutlier && "border-amber-300/70 bg-amber-50 shadow-[inset_0_0_0_1px_rgba(217,119,6,0.08)] dark:border-amber-700/50 dark:bg-amber-950/30",
      )}
      aria-label={
        isOutlier
          ? `${sourceLabel}: ${recorded ?? "—"} (different)`
          : `${sourceLabel}: ${recorded ?? "—"}`
      }
    >
      <span className="[overflow-wrap:anywhere]">{recorded ?? "—"}</span>
      {showCompared && (
        <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{compared}</span>
      )}
      {isOutlier && (
        <span className="inline-flex items-center gap-1 text-xs font-semibold text-amber-700 dark:text-amber-400">
          <AlertTriangle className="size-3" aria-hidden="true" />
          (different)
        </span>
      )}
    </span>
  );
}
