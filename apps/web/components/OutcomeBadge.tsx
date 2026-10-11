import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { outcomeLabel, type Outcome, type Source } from "@/lib/wording";

// contracts/components.md "OutcomeBadge"; colour per data-model.md §4. Text is never colour-only
// (NFR-09): aria-label always equals the visible text.
export type OutcomeBadgeProps = {
  outcome: Outcome;
  /** MISMATCHED → "{Outlier} is different" (null/undefined → "Sources disagree"). */
  outlier?: Source | null;
  /** MISSING → "Missing from ERP and Store sheet". */
  missingFrom?: Source[];
  /** SCR-02 counts: "21 · All three agree". */
  count?: number;
  className?: string;
};

// data-model.md §4: green MATCHED/MATCHED_AFTER_CONVERSION; red MISSING; amber MISMATCHED; blue
// NEEDS_REVIEW/UNMATCHED_NO_KEY; grey DUPLICATE/NON_RECEIPT.
const OUTCOME_COLOUR: Record<Outcome, string> = {
  MATCHED: "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300",
  MATCHED_AFTER_CONVERSION: "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300",
  MISMATCHED: "bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-300",
  MISSING: "bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300",
  NEEDS_REVIEW: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-300",
  UNMATCHED_NO_KEY: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-300",
  DUPLICATE: "bg-muted text-muted-foreground",
  NON_RECEIPT: "bg-muted text-muted-foreground",
};

export function OutcomeBadge({ outcome, outlier, missingFrom, count, className }: OutcomeBadgeProps) {
  const text = outcomeLabel(outcome, { outlier, missingFrom });
  const visible = count === undefined ? text : `${count} · ${text}`;
  return (
    <Badge variant="outline" aria-label={visible} className={cn(OUTCOME_COLOUR[outcome], "border-transparent", className)}>
      {visible}
    </Badge>
  );
}
