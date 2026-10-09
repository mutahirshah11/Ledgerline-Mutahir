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
  return (
    <p
      data-testid="proof-badge"
      data-status={status}
      className={cn(
        "inline-flex max-w-full items-center rounded-md px-2 py-1 text-sm font-medium [overflow-wrap:anywhere]",
        pass
          ? "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300"
          : "bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300",
        className,
      )}
    >
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
  return (
    <p
      data-testid={`proof-check-${id}`}
      data-status={pass ? "PASS" : "FAIL"}
      className={cn(
        "inline-flex max-w-full items-center gap-2 rounded-md px-2 py-1 text-sm font-medium [overflow-wrap:anywhere]",
        pass
          ? "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300"
          : "bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300",
      )}
    >
      {id} {pass ? "PASS" : "FAIL"} — {PROOF_CHECK_MEANING[id]}
    </p>
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
    <section data-testid="proof-panel-full" className={cn("flex flex-col gap-6", className)}>
      <div data-testid="proof-c1" className="flex flex-col gap-2">
        <CheckStatus id="C1" pass={C1.pass} />
        <table data-testid="proof-c1-table" className="w-full max-w-md text-sm">
          <thead>
            <tr className="text-left">
              <th scope="col">Source</th>
              <th scope="col">Rows in</th>
              <th scope="col">Rows placed</th>
            </tr>
          </thead>
          <tbody>
            {SOURCES.map((source) => {
              const row = C1.per_source[source];
              return (
                <tr key={source} data-testid={`proof-c1-row-${source}`}>
                  <th scope="row" className="text-left font-normal">{sourceLabel(source)}</th>
                  <td>{row?.in ?? 0}</td>
                  <td>{row?.placed ?? 0}</td>
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
      </div>

      <div data-testid="proof-c2" className="flex flex-col gap-2">
        <CheckStatus id="C2" pass={C2.pass} />
        <p>{placed} of {C2.rows} rows placed once</p>
        {C2.unplaced.length > 0 && <p>Unplaced: {C2.unplaced.join(", ")}</p>}
        {C2.placed_twice.length > 0 && <p>Placed twice: {C2.placed_twice.join(", ")}</p>}
      </div>

      <div data-testid="proof-c3" className="flex flex-col gap-2">
        <CheckStatus id="C3" pass={C3.pass} />
        <table data-testid="proof-c3-table" className="w-full max-w-md text-sm">
          <thead>
            <tr className="text-left">
              <th scope="col">Source</th>
              <th scope="col">Count</th>
            </tr>
          </thead>
          <tbody>
            {SOURCES.map((source) => (
              <tr key={source} data-testid={`proof-c3-row-${source}`}>
                <th scope="row" className="text-left font-normal">{sourceLabel(source)}</th>
                <td>{C3.per_source[source] ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {C3.problems.length > 0 && <p>{C3.problems.join(", ")}</p>}
      </div>

      <div data-testid="proof-c4" className="flex flex-col gap-2">
        <CheckStatus id="C4" pass={C4.pass} />
        <table data-testid="proof-c4-table" className="w-full max-w-md text-sm">
          <thead>
            <tr className="text-left">
              <th scope="col">Source</th>
              <th scope="col">Unit</th>
              <th scope="col">Qty in</th>
              <th scope="col">Qty placed</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(C4.by_source_unit).map(([key, balance]) => {
              const { source, unit } = parseSourceUnitKey(key);
              return (
                <tr key={key} data-testid={`proof-c4-row-${key}`}>
                  <th scope="row" className="text-left font-normal">{source}</th>
                  <td>{unit}</td>
                  <td>{balance.in}</td>
                  <td>{balance.placed}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {C4.qty_unknown.length > 0 && (
          <p data-testid="proof-c4-qty-unknown">
            Quantity unknown (not counted as zero): {C4.qty_unknown.join(", ")}
          </p>
        )}
      </div>

      <div data-testid="proof-c5" className="flex flex-col gap-2">
        <CheckStatus id="C5" pass={C5.pass} />
        <p data-testid="proof-c5-grns">{C5.grns} GRNs</p>
        <p data-testid="proof-c5-gaps">
          {C5.gaps.length > 0 ? `Gaps: ${C5.gaps.join(", ")}` : "No gaps"}
        </p>
      </div>
    </section>
  );
}

export function ProofPanel({ proof, mode = "compact", counts, className }: ProofPanelProps) {
  if (mode === "full") return <FullProofPanel proof={proof} counts={counts} className={className} />;
  return <CompactProofPanel proof={proof} className={className} />;
}
