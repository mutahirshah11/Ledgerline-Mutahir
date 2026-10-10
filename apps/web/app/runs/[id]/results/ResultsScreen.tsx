"use client";

// 006-web T054: SCR-05 results body (US5; FR-023/FR-024; contracts/components.md
// DiffCell/SourceTriplet). EP-11 groups, refetched from the browser whenever the outcome filter
// changes (`?outcome=` becomes the query param on the EP-11 call itself); GRN search narrows the
// already-fetched list client-side (EP-11 has no `?grn=` of its own). Expanding a row shows each
// source's raw values plus that row's EP-21 normalisation-log lines in plain words (the log is
// fetched once, unfiltered, and matched locally by `source_row_id` -- cheaper than one EP-21 call
// per row). Export CSV writes only the rows currently visible (lib/csv.ts, SCR-05-AC5).

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Fragment, useMemo, useState } from "react";

import { OutcomeBadge } from "@/components/OutcomeBadge";
import { SourceTriplet } from "@/components/SourceTriplet";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { toResultsCsv, type ResultsExportRow } from "@/lib/csv";
import { normLineText, type NormLogEntry } from "@/lib/norm";
import {
  cellsFor,
  groupDisplayField,
  itemOf,
  primaryDiff,
  supplierOf,
  toExportRow,
  SOURCES,
  type GroupView,
} from "@/lib/results";
import { outcomeLabel, sourceLabel, type Outcome } from "@/lib/wording";

const OUTCOMES: Outcome[] = [
  "MATCHED",
  "MATCHED_AFTER_CONVERSION",
  "MISMATCHED",
  "MISSING",
  "NEEDS_REVIEW",
  "UNMATCHED_NO_KEY",
  "DUPLICATE",
  "NON_RECEIPT",
];

function ExpandedGroup({ gv, norm }: { gv: GroupView; norm: NormLogEntry[] }) {
  return (
    <div className="flex flex-col gap-3 py-2 text-sm" data-testid="result-expand-body">
      {SOURCES.map((source) => {
        const row = gv.rows[source]?.[0];
        const rowIds = gv.group.row_ids[source] ?? [];
        const lines = norm.filter((n) => rowIds.includes(n.source_row_id));
        return (
          <div key={source} data-testid={`expand-${source}`}>
            <p className="font-medium">{sourceLabel(source)}</p>
            {row ? (
              <p className="text-muted-foreground [overflow-wrap:anywhere]">
                Date {row.date ?? "unknown"} · Supplier {row.supplier_code ?? "unknown"} · Lot{" "}
                {row.lot_no ?? "unknown"} · Qty {row.qty ?? "unknown"} {row.uom ?? ""}
              </p>
            ) : (
              <p className="italic text-muted-foreground">Not in {sourceLabel(source)}</p>
            )}
            {lines.length > 0 && (
              <ul className="list-disc pl-5">
                {lines.map((line, i) => (
                  <li key={`${line.source_row_id}:${line.code}:${i}`}>{normLineText(line, source)}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

function downloadCsv(filename: string, csv: string) {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function ResultsScreen({ runId }: { runId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const outcomeParam = (searchParams.get("outcome") ?? "") as Outcome | "";
  const grn = searchParams.get("grn") ?? "";
  const [expanded, setExpanded] = useState<string | null>(null);

  function setParam(name: string, value: string) {
    const params = new URLSearchParams(searchParams);
    if (value) params.set(name, value);
    else params.delete(name);
    const qs = params.toString();
    router.push(qs ? `?${qs}` : "?", { scroll: false });
  }

  const groupsQ = useQuery({
    queryKey: ["results", runId, "groups", outcomeParam || null],
    queryFn: ({ signal }) =>
      api.get("/runs/{id}/groups", {
        path: { id: runId },
        query: outcomeParam ? { outcome: outcomeParam } : undefined,
        signal,
      }),
  });

  const normQ = useQuery({
    queryKey: ["results", runId, "norm-log"],
    queryFn: ({ signal }) => api.get("/runs/{id}/normalisation-log", { path: { id: runId }, signal }),
  });

  const visible = useMemo(() => {
    const groups = groupsQ.data ?? [];
    if (!grn) return groups;
    return groups.filter((gv) => String(gv.group.grn_no ?? "").includes(grn));
  }, [groupsQ.data, grn]);

  const error = groupsQ.error ?? normQ.error;
  const norm = normQ.data ?? [];

  function exportCsv() {
    const rows: ResultsExportRow[] = visible.map(toExportRow);
    downloadCsv(`run-${runId}-results.csv`, toResultsCsv(rows));
  }

  return (
    <div className="mx-auto flex w-full min-w-0 max-w-5xl flex-col gap-4 px-3 py-4 sm:px-6">
      <p className="text-sm">
        <Link href={`/runs/${runId}`} className="underline underline-offset-2">
          Run overview
        </Link>
      </p>
      <h1 className="text-xl font-semibold">Results</h1>

      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="outcome-filter" className="text-sm font-medium">
            Outcome
          </label>
          <select
            id="outcome-filter"
            data-testid="outcome-filter"
            className="rounded border bg-background px-2 py-1.5 text-sm"
            value={outcomeParam}
            onChange={(e) => setParam("outcome", e.target.value)}
          >
            <option value="">All outcomes</option>
            {OUTCOMES.map((o) => (
              <option key={o} value={o}>
                {outcomeLabel(o)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="grn-search" className="text-sm font-medium">
            Search GRN
          </label>
          <Input
            id="grn-search"
            data-testid="grn-search"
            value={grn}
            onChange={(e) => setParam("grn", e.target.value)}
            className="w-32"
          />
        </div>
        <Button
          type="button"
          variant="outline"
          data-testid="export-csv"
          onClick={exportCsv}
          disabled={visible.length === 0}
        >
          Export CSV
        </Button>
      </div>

      {error ? (
        <ErrorState error={error instanceof ApiError ? error : { status: 0 }} />
      ) : !groupsQ.data ? (
        <Loading />
      ) : visible.length === 0 ? (
        <Empty text="No receipts match this filter." />
      ) : (
        <Table data-testid="results-table">
          <TableHeader>
            <TableRow>
              <TableHead>GRN</TableHead>
              <TableHead>Supplier</TableHead>
              <TableHead>Item</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead>Register / ERP / Store sheet</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((gv) => {
              const field = groupDisplayField(gv);
              const cells = cellsFor(gv, field);
              const diff = primaryDiff(gv.group);
              const key = gv.group.group_key;
              const isOpen = expanded === key;
              const toggle = () => setExpanded(isOpen ? null : key);
              return (
                <Fragment key={key}>
                  <TableRow
                    data-testid={`result-row-${gv.group.grn_no ?? key}`}
                    aria-expanded={isOpen}
                    tabIndex={0}
                    className="cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={toggle}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        toggle();
                      }
                    }}
                  >
                    <TableCell>{gv.group.grn_no ?? "—"}</TableCell>
                    <TableCell>{supplierOf(gv)}</TableCell>
                    <TableCell className="whitespace-normal [overflow-wrap:anywhere]">{itemOf(gv)}</TableCell>
                    <TableCell>
                      <OutcomeBadge
                        outcome={gv.group.outcome}
                        outlier={diff?.outlier}
                        missingFrom={gv.group.missing_from}
                      />
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <SourceTriplet field={field} cells={cells} />
                    </TableCell>
                  </TableRow>
                  {isOpen && (
                    <TableRow data-testid={`result-expand-${gv.group.grn_no ?? key}`}>
                      <TableCell colSpan={5} className="whitespace-normal">
                        <ExpandedGroup gv={gv} norm={norm} />
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
