"use client";

// 006-web T057 (US6, SCR-02, contracts/screens.md "SCR-02"): the client table for the runs list.
// Rows are keyboard-activatable (tabIndex + Enter, same target as the click handler — contracts
// don't mandate a real per-row <a>, and a row spans several <td>s, so this is this task's own
// choice over wrapping the whole row in a link). "New run" only for a `can.createRun` person
// (CONTROLLER, lib/me.ts); the API stays the authority either way.

import { createColumnHelper, tableFeatures, useTable } from "@tanstack/react-table";
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
  columnHelper.accessor("month", { header: "Month" }),
  columnHelper.display({
    id: "status",
    header: "Status",
    cell: (info) => <RunStatusBadge status={info.row.original.status} />,
  }),
  columnHelper.accessor("created_at", {
    header: "Created",
    cell: (info) => <time dateTime={info.getValue()}>{info.getValue()}</time>,
  }),
  columnHelper.display({
    id: "outcomes",
    header: "Outcomes",
    cell: (info) => (
      <div className="flex flex-wrap gap-1">
        {nonZeroOutcomes(info.row.original.counts).map(({ outcome, count }) => (
          <OutcomeBadge key={outcome} outcome={outcome} count={count} />
        ))}
      </div>
    ),
  }),
  columnHelper.display({
    id: "proof",
    header: "Proof",
    cell: (info) => proofText(info.row.original.proof_status),
  }),
]);

export function RunsTable({ runs }: RunsTableProps) {
  const router = useRouter();
  const { can } = useMe();
  const table = useTable({ features, columns, data: runs });

  const newRunLink = can.createRun ? (
    <Button render={<Link href="/runs/new" />}>New run</Button>
  ) : null;

  if (runs.length === 0) {
    return <Empty text="No runs yet." action={newRunLink} />;
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Runs</h1>
        {newRunLink}
      </div>
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id}>
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
                className="cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={open}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    open();
                  }
                }}
              >
                {row.getAllCells().map((cell) => (
                  <TableCell key={cell.id}>
                    <table.FlexRender cell={cell} />
                  </TableCell>
                ))}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
