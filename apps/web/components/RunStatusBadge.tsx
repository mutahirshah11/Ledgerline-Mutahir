import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { runStatusLabel, type RunStatus, type Stage } from "@/lib/wording";

// contracts/components.md "RunStatusBadge". Text is never colour-only (NFR-09): aria-label always
// equals the visible text.
export type RunStatusBadgeProps = {
  status: RunStatus;
  failedStage?: Stage | null;
  className?: string;
};

// No colour is specified for RunStatus in contracts/ui-copy.md or data-model.md (only outcome
// colours are specified there) — this scheme is this task's own choice, noted in the 006-web
// Phase 2 report: grey while the run is still being built/read, blue once it needs/awaits human
// attention, green once it is ready/done, red on FAILED.
const RUN_STATUS_COLOUR: Record<RunStatus, string> = {
  CREATED: "bg-muted text-muted-foreground",
  UPLOADED: "bg-muted text-muted-foreground",
  VALIDATED: "bg-muted text-muted-foreground",
  EXTRACTING: "bg-muted text-muted-foreground",
  EXTRACTED: "bg-muted text-muted-foreground",
  RECONCILED: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-300",
  AWAITING_REVIEW: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-300",
  READY: "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300",
  FINALISED: "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300",
  FAILED: "bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300",
};

export function RunStatusBadge({ status, failedStage, className }: RunStatusBadgeProps) {
  const text = runStatusLabel(status, failedStage);
  return (
    <Badge
      variant="outline"
      aria-label={text}
      className={cn(RUN_STATUS_COLOUR[status], "border-transparent", className)}
    >
      {text}
    </Badge>
  );
}
