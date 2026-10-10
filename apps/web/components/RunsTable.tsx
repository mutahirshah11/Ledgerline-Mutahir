"use client";

// 2026 redesign of SCR-02 (the Runs dashboard): hero health summary + "how this month was
// checked" + clickable stat cards + the real "every month at a glance" table (MonthlyTable,
// which owns every SCR-02 contract requirement — testids, aria-labels, row navigation) + this
// run's activity. "New run" stays gated by `can.createRun` exactly as before.

import { Plus } from "lucide-react";
import Link from "next/link";

import { useMe } from "@/app/providers";
import { Button } from "@/components/ui/button";
import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { DashboardHero } from "@/components/dashboard/DashboardHero";
import { MonthlyTable } from "@/components/dashboard/MonthlyTable";
import { RunsEmptyState } from "@/components/dashboard/RunsEmptyState";
import { SourceCheckCard } from "@/components/dashboard/SourceCheckCard";
import { SummaryStats } from "@/components/dashboard/SummaryStats";
import type { WaffleCell } from "@/components/dashboard/Waffle";
import type { components } from "@/lib/api-types";

export type RunSummary = components["schemas"]["RunSummary"];
export type RunView = components["schemas"]["RunView"];
export type GroupView = components["schemas"]["GroupView"];
export type AuditEntryOut = components["schemas"]["AuditEntryOut"];

export type RunsTableProps = {
  runs: RunSummary[];
  /** The newest run's full detail, groups (for the waffle) and audit trail — best-effort: any of
   * these can be null if that extra read failed, and the dashboard still renders without it. */
  latest: RunView | null;
  latestGroups: GroupView[] | null;
  latestAudit: AuditEntryOut[] | null;
};

function waffleCells(run: RunView, groups: GroupView[] | null): WaffleCell[] {
  if (!groups || groups.length === 0) return [];
  return groups.map(({ group }): WaffleCell => {
    if (group.outcome === "MATCHED" || group.outcome === "MATCHED_AFTER_CONVERSION") return "matched";
    if (run.status === "FINALISED") return "resolved";
    return "needs";
  });
}

export function RunsTable({ runs, latest, latestGroups, latestAudit }: RunsTableProps) {
  const { can } = useMe();

  const newRunLink = can.createRun ? (
    <Button render={<Link href="/runs/new" />} className="gap-2 bg-primary hover:bg-primary-hover">
      <Plus className="h-4 w-4" aria-hidden="true" />
      New run
    </Button>
  ) : null;

  if (runs.length === 0) {
    return <RunsEmptyState canCreateRun={can.createRun} />;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm text-muted-foreground">Good to see you</p>
          <h1 className="font-heading text-3xl font-bold tracking-tight text-foreground">Reconciliation runs</h1>
        </div>
        {newRunLink}
      </div>

      {latest ? <DashboardHero run={latest} cells={waffleCells(latest, latestGroups)} /> : null}
      {latest ? <SourceCheckCard run={latest} /> : null}

      <SummaryStats runs={runs} />

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_320px]">
        <MonthlyTable runs={runs} />
        <ActivityFeed entries={latestAudit ?? []} />
      </div>
    </div>
  );
}
