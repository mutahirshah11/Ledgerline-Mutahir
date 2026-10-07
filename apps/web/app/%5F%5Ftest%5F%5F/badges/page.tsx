import { notFound } from "next/navigation";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import type { Outcome, RunStatus, Source, Stage } from "@/lib/wording";

// 006-web T023: this page only exists so Playwright can render OutcomeBadge/RunStatusBadge
// outside a full screen (T014). It must never serve outside the `screens` Playwright webServer,
// which is the only thing that sets ENABLE_TEST_PAGES=1 (playwright.config.ts). `force-dynamic`
// stops Next from statically prerendering the page (and baking in whatever ENABLE_TEST_PAGES was
// at build time) — the env var is read fresh on every request instead.
export const dynamic = "force-dynamic";

const OUTCOMES: {
  outcome: Outcome;
  outlier?: Source | null;
  missingFrom?: Source[];
  count?: number;
}[] = [
  { outcome: "MATCHED" },
  { outcome: "MATCHED_AFTER_CONVERSION" },
  { outcome: "MISMATCHED", outlier: "ERP" },
  { outcome: "MISMATCHED", outlier: null },
  { outcome: "MISSING", missingFrom: ["REGISTER", "SHEET"] },
  { outcome: "NEEDS_REVIEW" },
  { outcome: "UNMATCHED_NO_KEY" },
  { outcome: "DUPLICATE" },
  { outcome: "NON_RECEIPT" },
  { outcome: "MATCHED", count: 21 },
];

const RUN_STATUSES: { status: RunStatus; failedStage?: Stage | null }[] = [
  { status: "CREATED" },
  { status: "UPLOADED" },
  { status: "VALIDATED" },
  { status: "EXTRACTING" },
  { status: "EXTRACTED" },
  { status: "RECONCILED" },
  { status: "AWAITING_REVIEW" },
  { status: "READY" },
  { status: "FINALISED" },
  { status: "FAILED", failedStage: "extract" },
];

export default function BadgesTestPage() {
  if (process.env.ENABLE_TEST_PAGES !== "1") notFound();

  return (
    <main>
      <h1>Badges test page</h1>
      <section aria-label="outcomes">
        {OUTCOMES.map((o, i) => (
          <div key={i} data-testid={`outcome-${i}`}>
            <OutcomeBadge
              outcome={o.outcome}
              outlier={o.outlier}
              missingFrom={o.missingFrom}
              count={o.count}
            />
          </div>
        ))}
      </section>
      <section aria-label="run-statuses">
        {RUN_STATUSES.map((s, i) => (
          <div key={i} data-testid={`run-status-${i}`}>
            <RunStatusBadge status={s.status} failedStage={s.failedStage} />
          </div>
        ))}
      </section>
    </main>
  );
}
