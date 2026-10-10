import { CheckCircle2, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import type { components } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { type Outcome, type Source, sourceLabel } from "@/lib/wording";

import { OutcomeBadge } from "./OutcomeBadge";

// contracts/components.md "ProofPanel" (006-web T039). This file renders the compact one-liner
// for SCR-04 (SCR-04-AC2, B 19); US7 (T061) adds `mode: "full"` for SCR-07 (all five checks with
// their numbers) and the optional `counts` it needs, without changing compact callers.

export type Proof = components["schemas"]["Proof"];
export type ProofCheckId = "C1" | "C2" | "C3" | "C4" | "C5";
export type ProofPanelMode = "compact" | "full";

export type ProofPanelProps = {
  proof: Proof;
  /** "compact" (default, SCR-04): one line. "full" (SCR-07): all five checks with their numbers. */
  mode?: ProofPanelMode;
  /** EP-05 `counts` (rows by outcome), shown beside C1 in full mode for context (contracts/screens.md
   * SCR-07 "by outcome"). Ignored in compact mode. */
  counts?: Partial<Record<Outcome, number>>;
  className?: string;
};

export const PROOF_CHECK_IDS: readonly ProofCheckId[] = ["C1", "C2", "C3", "C4", "C5"];

/** ui-copy.md §6: what each check means when it passes. */
export const PROOF_CHECK_MEANING: Record<ProofCheckId, string> = {
  C1: "Every row read was placed",
  C2: "Each row placed exactly once",
  C3: "Row counts agree when counted a second way",
  C4: "Quantity totals tie",
  C5: "Every GRN accounted for",
};

/** The first failing check in C1..C5 order, or null when all pass. */
export function firstFailingCheck(proof: Proof): ProofCheckId | null {
  return PROOF_CHECK_IDS.find((id) => !proof.checks[id].pass) ?? null;
}

/** ui-copy.md §4: "Proof: {placed} of {rows} rows placed once" / "Proof failed: {check meaning}".
 * Row counts are integers from C2 (rows, unplaced, placed_twice), not quantities. */
export function proofLine(proof: Proof): string {
  if (proof.status !== "PASS") {
    return `Proof failed: ${PROOF_CHECK_MEANING[firstFailingCheck(proof) ?? "C1"]}`;
  }
  const { rows, unplaced, placed_twice } = proof.checks.C2;
  const placed = rows - unplaced.length - placed_twice.length;
  return `Proof: ${placed} of ${rows} rows placed once`;
}

/** The compact badge's markup, reused by the fallback (EP-13 unreadable: `run.proof_status` only,
 * no `Proof` to compute `proofLine` from — FR-016). */
export function ProofStatusBadge({
  status,
  text,
  className,
}: {
  status: components["schemas"]["ProofStatus"];
  text: string;
  className?: string;
}) {
  const pass = status === "PASS";
  const Icon = pass ? ShieldCheck : ShieldAlert;
  return (
    <p
      data-testid="proof-badge"
      data-status={status}
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-semibold [overflow-wrap:anywhere]",
        pass
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
          : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300",
        className,
      )}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      {text}
    </p>
  );
}

function CompactProofPanel({ proof, className }: { proof: Proof; className?: string }) {
  return <ProofStatusBadge status={proof.status} text={proofLine(proof)} className={className} />;
}

const SOURCES: readonly Source[] = ["REGISTER", "ERP", "SHEET"];

/** PASS/FAIL in text and colour (NFR-09: never colour alone), with the check's one-line meaning. */
function CheckStatus({ id, pass }: { id: ProofCheckId; pass: boolean }) {
  const Icon = pass ? CheckCircle2 : XCircle;
  return (
    <p
      data-testid={`proof-check-${id}`}
      data-status={pass ? "PASS" : "FAIL"}
      className={cn(
        "inline-flex max-w-full items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-semibold [overflow-wrap:anywhere]",
        pass
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
          : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300",
      )}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      {id} {pass ? "PASS" : "FAIL"} — {PROOF_CHECK_MEANING[id]}
    </p>
  );
}

/** Shared card chrome for each C1..C5 block: a left accent bar that reads PASS/FAIL at a glance. */
function CheckCard({ pass, children }: { pass: boolean; children: ReactNode }) {
  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-xl border bg-white p-4 shadow-sm dark:bg-card",
        pass ? "border-l-4 border-l-emerald-500" : "border-l-4 border-l-red-500",
      )}
    >
      {children}
    </div>
  );
}

/** Small-caps table header row, reused by the C1/C3/C4 tables below. */
function TableHead({ cols }: { cols: string[] }) {
  return (
    <thead>
      <tr className="border-b text-left text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {cols.map((c) => (
          <th key={c} scope="col" className="py-1.5 pr-4">
            {c}
          </th>
        ))}
      </tr>
    </thead>
  );
}

function parseSourceUnitKey(key: string): { source: string; unit: string } {
  const i = key.indexOf("|");
  return { source: key.slice(0, i), unit: key.slice(i + 1) };
}

function FullProofPanel({ proof, counts, className }: { proof: Proof; counts?: Partial<Record<Outcome, number>>; className?: string }) {
  const { C1, C2, C3, C4, C5 } = proof.checks;
  const placed = C2.rows - C2.unplaced.length - C2.placed_twice.length;
  const byOutcome = Object.entries(counts ?? {}).filter(([, n]) => (n ?? 0) > 0) as [Outcome, number][];
  return (
    <section data-testid="proof-panel-full" className={cn("flex flex-col gap-4", className)}>
      <div data-testid="proof-c1">
        <CheckCard pass={C1.pass}>
          <CheckStatus id="C1" pass={C1.pass} />
          <table data-testid="proof-c1-table" className="w-full max-w-md border-collapse text-sm">
            <TableHead cols={["Source", "Rows in", "Rows placed"]} />
            <tbody>
              {SOURCES.map((source) => {
                const row = C1.per_source[source];
                return (
                  <tr key={source} data-testid={`proof-c1-row-${source}`} className="border-b last:border-0">
                    <th scope="row" className="py-1.5 pr-4 text-left font-normal">{sourceLabel(source)}</th>
                    <td className="py-1.5 pr-4 tabular-nums">{row?.in ?? 0}</td>
                    <td className="py-1.5 tabular-nums">{row?.placed ?? 0}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {byOutcome.length > 0 && (
            <div className="flex flex-wrap gap-1" aria-label="Rows by outcome">
              {byOutcome.map(([outcome, count]) => (
                <OutcomeBadge key={outcome} outcome={outcome} count={count} />
              ))}
            </div>
          )}
        </CheckCard>
      </div>

      <div data-testid="proof-c2">
        <CheckCard pass={C2.pass}>
          <CheckStatus id="C2" pass={C2.pass} />
          <p>{placed} of {C2.rows} rows placed once</p>
          {C2.unplaced.length > 0 && <p>Unplaced: {C2.unplaced.join(", ")}</p>}
          {C2.placed_twice.length > 0 && <p>Placed twice: {C2.placed_twice.join(", ")}</p>}
        </CheckCard>
      </div>

      <div data-testid="proof-c3">
        <CheckCard pass={C3.pass}>
          <CheckStatus id="C3" pass={C3.pass} />
          <table data-testid="proof-c3-table" className="w-full max-w-md border-collapse text-sm">
            <TableHead cols={["Source", "Count"]} />
            <tbody>
              {SOURCES.map((source) => (
                <tr key={source} data-testid={`proof-c3-row-${source}`} className="border-b last:border-0">
                  <th scope="row" className="py-1.5 pr-4 text-left font-normal">{sourceLabel(source)}</th>
                  <td className="py-1.5 tabular-nums">{C3.per_source[source] ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {C3.problems.length > 0 && <p>{C3.problems.join(", ")}</p>}
        </CheckCard>
      </div>

      <div data-testid="proof-c4">
        <CheckCard pass={C4.pass}>
          <CheckStatus id="C4" pass={C4.pass} />
          <table data-testid="proof-c4-table" className="w-full max-w-md border-collapse text-sm">
            <TableHead cols={["Source", "Unit", "Qty in", "Qty placed"]} />
            <tbody>
              {Object.entries(C4.by_source_unit).map(([key, balance]) => {
                const { source, unit } = parseSourceUnitKey(key);
                return (
                  <tr key={key} data-testid={`proof-c4-row-${key}`} className="border-b last:border-0">
                    <th scope="row" className="py-1.5 pr-4 text-left font-normal">{source}</th>
                    <td className="py-1.5 pr-4">{unit}</td>
                    <td className="py-1.5 pr-4 tabular-nums">{balance.in}</td>
                    <td className="py-1.5 tabular-nums">{balance.placed}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {C4.qty_unknown.length > 0 && (
            <p data-testid="proof-c4-qty-unknown" className="text-amber-800 dark:text-amber-300">
              Quantity unknown (not counted as zero): {C4.qty_unknown.join(", ")}
            </p>
          )}
        </CheckCard>
      </div>

      <div data-testid="proof-c5">
        <CheckCard pass={C5.pass}>
          <CheckStatus id="C5" pass={C5.pass} />
          <p data-testid="proof-c5-grns" className="text-sm">{C5.grns} GRNs</p>
          <p data-testid="proof-c5-gaps" className="text-sm">
            {C5.gaps.length > 0 ? `Gaps: ${C5.gaps.join(", ")}` : "No gaps"}
          </p>
        </CheckCard>
      </div>
    </section>
  );
}

export function ProofPanel({ proof, mode = "compact", counts, className }: ProofPanelProps) {
  if (mode === "full") return <FullProofPanel proof={proof} counts={counts} className={className} />;
  return <CompactProofPanel proof={proof} className={className} />;
}
