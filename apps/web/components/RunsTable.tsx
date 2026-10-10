"use client";

// 006-web T057 (US6, SCR-02, contracts/screens.md "SCR-02"): the client table for the runs list.
// Rows are keyboard-activatable (tabIndex + Enter, same target as the click handler — contracts
// don't mandate a real per-row <a>, and a row spans several <td>s, so this is this task's own
// choice over wrapping the whole row in a link). "New run" only for a `can.createRun` person
// (CONTROLLER, lib/me.ts); the API stays the authority either way.

import { createColumnHelper, tableFeatures, useTable } from "@tanstack/react-table";
import { CheckCircle2, CircleSlash, FolderOpen, Plus, XCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { useMe } from "@/app/providers";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Empty } from "@/components/states";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import { cn } from "@/lib/utils";
import type { components } from "@/lib/api-types";
import type { Outcome } from "@/lib/wording";

export type RunSummary = components["schemas"]["RunSummary"];

export type RunsTableProps = {
  runs: RunSummary[];
};

function proofText(status: RunSummary["proof_status"]): string {
  if (status === "PASS") return "Proof passed";
  if (status === "FAIL") return "Proof failed";
  return "Not run yet";
}

function ProofCell({ status }: { status: RunSummary["proof_status"] }) {
  const Icon = status === "PASS" ? CheckCircle2 : status === "FAIL" ? XCircle : CircleSlash;
  const tone =
    status === "PASS"
      ? "text-green-700 dark:text-green-400"
      : status === "FAIL"
        ? "text-red-700 dark:text-red-400"
        : "text-muted-foreground";
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-sm", tone)}>
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      {proofText(status)}
    </span>
  );
}

function nonZeroOutcomes(counts: RunSummary["counts"]): { outcome: Outcome; count: number }[] {
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([outcome, count]) => ({ outcome: outcome as Outcome, count }));
}

// @tanstack/react-table v9 (table-core skills/getting-started/SKILL.md "Setup"): `tableFeatures`
// picks which optional state/row-model slots exist (none needed — the list is unfiltered,
// unsorted, unpaginated, EP-04 does the ordering); `useTable` always builds the core row model.
const features = tableFeatures({});
const columnHelper = createColumnHelper<typeof features, RunSummary>();
const columns = columnHelper.columns([
  columnHelper.accessor("month", {
    header: "Month",
    cell: (info) => <span className="font-medium text-foreground">{info.getValue()}</span>,
  }),
  columnHelper.display({
    id: "status",
    header: "Status",
    cell: (info) => <RunStatusBadge status={info.row.original.status} />,
  }),
  columnHelper.accessor("created_at", {
    header: "Created",
    cell: (info) => (
      <time dateTime={info.getValue()} className="text-muted-foreground">
        {info.getValue()}
      </time>
    ),
  }),
  columnHelper.display({
    id: "outcomes",
    header: "Outcomes",
    cell: (info) => (
      <div className="flex flex-wrap gap-1.5">
        {nonZeroOutcomes(info.row.original.counts).map(({ outcome, count }) => (
          <OutcomeBadge key={outcome} outcome={outcome} count={count} />
        ))}
      </div>
    ),
  }),
  columnHelper.display({
    id: "proof",
    header: "Proof",
    cell: (info) => <ProofCell status={info.row.original.proof_status} />,
  }),
]);

export function RunsTable({ runs }: RunsTableProps) {
  const router = useRouter();
  const { can } = useMe();
  const table = useTable({ features, columns, data: runs });

  const newRunLink = can.createRun ? (
    <Button render={<Link href="/runs/new" />} className="bg-emerald-700 hover:bg-emerald-700/90">
      <Plus className="h-4 w-4" aria-hidden="true" />
      New run
    </Button>
  ) : null;

  if (runs.length === 0) {
    return (
      <div className="flex flex-col items-center gap-4 rounded-xl border border-dashed border-border bg-muted/20 px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-400">
          <FolderOpen className="h-6 w-6" aria-hidden="true" />
        </div>
        <Empty text="No runs yet." action={newRunLink} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">Runs</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Reconciliation runs for Kestrel Industries, newest first.
          </p>
        </div>
        {newRunLink}
      </div>
      <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id} className="hover:bg-transparent">
                {headerGroup.headers.map((header) => (
                  <TableHead
                    key={header.id}
                    className="h-11 bg-muted/40 text-xs font-semibold tracking-wide text-muted-foreground uppercase"
                  >
                    {header.isPlaceholder ? null : <table.FlexRender header={header} />}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.map((row) => {
              const run = row.original;
              const href = `/runs/${run.id}`;
              const open = () => router.push(href);
              return (
                <TableRow
                  key={row.id}
                  data-testid={`run-row-${run.id}`}
                  tabIndex={0}
                  aria-label={`Open run ${run.month}`}
                  className="cursor-pointer transition-colors hover:bg-emerald-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-inset dark:hover:bg-emerald-950/20"
                  onClick={open}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      open();
                    }
                  }}
                >
                  {row.getAllCells().map((cell) => (
                    <TableCell key={cell.id} className="py-3">
                      <table.FlexRender cell={cell} />
                    </TableCell>
                  ))}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
