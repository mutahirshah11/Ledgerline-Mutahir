// 006-web read cost on SCR-03 (spec.md Clarifications 2026-10-07; data-model.md "Read cost";
// ui-copy.md "Read cost"). Shows the API's numbers as received (decimal strings, never parsed):
// before reading "est. US${estimate}", after reading "Read cost US${actual}"; "Read cost not
// available" while the prices are unset (null). Never says whether a page's read was cached.

import { readCostActualLine, readCostEstimateLine } from "@/lib/run-steps";

export type ReadCostProps = {
  /** From EP-07 (`new_pages.length`, `read_cost_estimate_usd`) or EP-05 `read_cost`. */
  estimate: { newPages: number | null; usd: string | null } | null;
  /** EP-05 `read_cost.actual_usd` once pages have been read; undefined before. */
  actualUsd?: string | null;
};

export function ReadCost({ estimate, actualUsd }: ReadCostProps) {
  if (!estimate && actualUsd === undefined) return null;
  return (
    <section aria-label="Read cost" className="rounded-xl border border-border bg-muted/40 p-3.5 text-sm">
      {estimate ? <p>{readCostEstimateLine(estimate.usd)}</p> : null}
      {actualUsd !== undefined ? <p className="font-medium">{readCostActualLine(actualUsd)}</p> : null}
    </section>
  );
}
