import { Activity } from "lucide-react";

import { formatRelative } from "@/lib/time";
import { auditActionLabel } from "@/lib/wording";
import type { components } from "@/lib/api-types";

type AuditEntryOut = components["schemas"]["AuditEntryOut"];

export type ActivityFeedProps = {
  /** Newest-first audit trail for the dashboard's current/latest run (no cross-run activity
   * endpoint exists, so this is scoped to that one run rather than the whole workspace). */
  entries: AuditEntryOut[];
};

export function ActivityFeed({ entries }: ActivityFeedProps) {
  return (
    <div className="rounded-2xl border border-border bg-card p-5 shadow-sm">
      <h3 className="font-heading text-base font-bold tracking-tight text-foreground">What&apos;s been happening</h3>
      {entries.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">Nothing recorded for this run yet.</p>
      ) : (
        <ul className="mt-3 flex flex-col gap-3">
          {entries.map((entry) => (
            <li key={entry.id} className="flex items-start gap-3">
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary-soft text-primary">
                <Activity className="h-3.5 w-3.5" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <p className="text-sm text-foreground">
                  <span className="font-medium">{entry.actor}</span> — {auditActionLabel(entry.action)}
                </p>
                <p className="text-xs text-muted-foreground">{formatRelative(entry.at)}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
