import type { components } from "@/lib/api-types";
import { cn } from "@/lib/utils";

// contracts/components.md "ProofPanel" (006-web T039). This file renders the compact one-liner
// for SCR-04 (SCR-04-AC2, B 19); US7 (T061) adds `mode: "full"` for SCR-07 (all five checks with
// their numbers) and the optional `counts` it needs, without changing compact callers.

export type Proof = components["schemas"]["Proof"];
export type ProofCheckId = "C1" | "C2" | "C3" | "C4" | "C5";
export type ProofPanelMode = "compact"; // US7 widens this to "compact" | "full"

export type ProofPanelProps = {
  proof: Proof;
  /** "compact" (default, SCR-04): one line. */
  mode?: ProofPanelMode;
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

export function ProofPanel({ proof, className }: ProofPanelProps) {
  const pass = proof.status === "PASS";
  return (
    <p
      data-testid="proof-badge"
      data-status={proof.status}
      className={cn(
        "inline-flex max-w-full items-center rounded-md px-2 py-1 text-sm font-medium [overflow-wrap:anywhere]",
        pass
          ? "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300"
          : "bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300",
        className,
      )}
    >
      {proofLine(proof)}
    </p>
  );
}
