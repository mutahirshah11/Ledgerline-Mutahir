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
        "inline-flex flex-col gap-0.5",
        differs && "rounded bg-amber-100 px-1 dark:bg-amber-900/30",
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
        <span className="text-xs font-medium text-amber-700 dark:text-amber-400">
          (different)
        </span>
      )}
    </span>
  );
}
