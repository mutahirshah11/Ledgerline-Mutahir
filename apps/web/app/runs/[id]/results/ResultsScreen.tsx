"use client";

// 006-web T054: SCR-05 results body (US5; FR-023/FR-024; contracts/components.md
// DiffCell/SourceTriplet). EP-11 groups, refetched from the browser whenever the outcome filter
// changes (`?outcome=` becomes the query param on the EP-11 call itself); GRN search narrows the
// already-fetched list client-side (EP-11 has no `?grn=` of its own). Expanding a row shows each
// source's raw values plus that row's EP-21 normalisation-log lines in plain words (the log is
// fetched once, unfiltered, and matched locally by `source_row_id` -- cheaper than one EP-21 call
// per row). Export CSV writes only the rows currently visible (lib/csv.ts, SCR-05-AC5).

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronRight, Download, Search } from "lucide-react";
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
import { cn } from "@/lib/utils";
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
    <div
      className="grid grid-cols-1 gap-4 py-3 text-sm sm:grid-cols-3"
      data-testid="result-expand-body"
    >
      {SOURCES.map((source) => {
        const row = gv.rows[source]?.[0];
        const rowIds = gv.group.row_ids[source] ?? [];
        const lines = norm.filter((n) => rowIds.includes(n.source_row_id));
        return (
          <div
            key={source}
            data-testid={`expand-${source}`}
            className="flex flex-col gap-1.5 rounded-lg border border-border/60 bg-background/60 p-3"
          >
            <p className="text-[11px] font-semibold tracking-wide text-emerald-700 uppercase dark:text-emerald-400">
              {sourceLabel(source)}
            </p>
            {row ? (
              <p className="text-muted-foreground [overflow-wrap:anywhere]">
                Date {row.date ?? "unknown"} · Supplier {row.supplier_code ?? "unknown"} · Lot{" "}
                {row.lot_no ?? "unknown"} · Qty {row.qty ?? "unknown"} {row.uom ?? ""}
              </p>
            ) : (
              <p className="italic text-muted-foreground">Not in {sourceLabel(source)}</p>
            )}
            {lines.length > 0 && (
              <ul className="flex flex-col gap-1 border-t border-border/60 pt-1.5 text-xs text-muted-foreground">
                {lines.map((line, i) => (
                  <li key={`${line.source_row_id}:${line.code}:${i}`} className="flex gap-1.5">
                    <span className="mt-1 size-1 shrink-0 rounded-full bg-emerald-500/70" aria-hidden="true" />
                    <span>{normLineText(line, source)}</span>
                  </li>
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
    <div className="mx-auto flex w-full min-w-0 max-w-6xl flex-col gap-5 px-3 py-6 sm:px-6">
      <div className="flex flex-col gap-1">
        <Link
          href={`/runs/${runId}`}
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          <ArrowLeft className="size-3.5" aria-hidden="true" />
          Run overview
        </Link>
        <div className="flex items-baseline justify-between gap-3">
          <h1 className="font-[family-name:var(--font-fraunces)] text-2xl font-medium tracking-tight">Results</h1>
          {groupsQ.data && (
            <p className="text-sm text-muted-foreground">
              {visible.length} of {groupsQ.data.length} receipts
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-border/70 bg-card p-4 shadow-sm">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="outcome-filter" className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            Outcome
          </label>
          <select
            id="outcome-filter"
            data-testid="outcome-filter"
            className="h-9 rounded-md border border-input bg-background px-2.5 text-sm shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
        <div className="flex flex-col gap-1.5">
          <label htmlFor="grn-search" className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            Search GRN
          </label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input
              id="grn-search"
              data-testid="grn-search"
              value={grn}
              onChange={(e) => setParam("grn", e.target.value)}
              className="w-36 pl-8"
            />
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          data-testid="export-csv"
          onClick={exportCsv}
          disabled={visible.length === 0}
          className="ml-auto gap-1.5"
        >
          <Download className="size-3.5" aria-hidden="true" />
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
        <div className="overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm">
          <Table data-testid="results-table">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">GRN</TableHead>
                <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Supplier</TableHead>
                <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Item</TableHead>
                <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Outcome</TableHead>
                <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Register / ERP / Store sheet</TableHead>
                <TableHead className="w-8 bg-muted/40" aria-hidden="true" />
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
                      className={cn(
                        "cursor-pointer align-top transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
                        isOpen && "bg-emerald-50/60 dark:bg-emerald-950/15",
                      )}
                      onClick={toggle}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          toggle();
                        }
                      }}
                    >
                      <TableCell className="font-medium">{gv.group.grn_no ?? "—"}</TableCell>
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
                      <TableCell>
                        <ChevronRight
                          className={cn("size-4 text-muted-foreground transition-transform", isOpen && "rotate-90")}
                          aria-hidden="true"
                        />
                      </TableCell>
                    </TableRow>
                    {isOpen && (
                      <TableRow
                        data-testid={`result-expand-${gv.group.grn_no ?? key}`}
                        className="hover:bg-transparent"
                      >
                        <TableCell colSpan={6} className="whitespace-normal bg-muted/20 px-4">
                          <ExpandedGroup gv={gv} norm={norm} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
