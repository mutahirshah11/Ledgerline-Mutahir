import { CheckCircle2, ClipboardList, Flag, XCircle } from "lucide-react";
import Link from "next/link";

import type { RunSummary } from "@/components/RunsTable";

export type SummaryStatsProps = {
  runs: RunSummary[];
};

function isMatched(run: RunSummary): boolean {
  const total = Object.values(run.counts).reduce((s, n) => s + n, 0);
  const matched = (run.counts.MATCHED ?? 0) + (run.counts.MATCHED_AFTER_CONVERSION ?? 0);
  return run.status === "FINALISED" && total > 0 && matched === total;
}

export function SummaryStats({ runs }: SummaryStatsProps) {
  const total = runs.length;
  const allMatched = runs.filter(isMatched);
  const needsYou = runs.find((r) => r.status !== "FAILED" && r.status !== "FINALISED");
  const didntFinish = runs.find((r) => r.status === "FAILED");

  const months = runs.map((r) => r.month).sort();
  const span = months.length > 1 ? `${months[0]} to ${months[months.length - 1]}` : months[0];

  const cards: Array<{
    icon: typeof CheckCircle2;
    tone: string;
    value: number | string;
    label: string;
    sub: string;
    href?: string;
  }> = [
    {
      icon: ClipboardList,
      tone: "bg-primary-soft text-primary",
      value: total,
      label: "Runs",
      sub: span ?? "—",
    },
    {
      icon: CheckCircle2,
      tone: "bg-success-soft text-success-ink",
      value: allMatched.length,
      label: "All matched",
      sub: allMatched.length > 0 ? "Fully reconciled" : "None yet",
    },
    {
      icon: Flag,
      tone: "bg-warning-soft text-warning-ink",
      value: needsYou ? 1 : 0,
      label: "Needs you",
      sub: needsYou ? needsYou.month : "Nothing pending",
      href: needsYou ? `/runs/${needsYou.id}` : undefined,
    },
    {
      icon: XCircle,
      tone: "bg-danger-soft text-danger-ink",
      value: didntFinish ? 1 : 0,
      label: "Didn't finish",
      sub: didntFinish ? didntFinish.month : "None",
      href: didntFinish ? `/runs/${didntFinish.id}` : undefined,
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {cards.map((card) => {
        const Icon = card.icon;
        const body = (
          <>
            <span className={`flex h-9 w-9 items-center justify-center rounded-lg ${card.tone}`}>
              <Icon className="h-4.5 w-4.5" aria-hidden="true" />
            </span>
            <p className="mt-3 font-heading text-2xl font-bold tabular-nums text-foreground">{card.value}</p>
            <p className="text-sm font-medium text-foreground">{card.label}</p>
            <p className="truncate text-xs text-muted-foreground">{card.sub}</p>
          </>
        );
        const className =
          "flex flex-col items-start rounded-2xl border border-border bg-card p-4 text-left shadow-sm transition-colors hover:bg-muted/40";
        return card.href ? (
          <Link key={card.label} href={card.href} className={className}>
            {body}
          </Link>
        ) : (
          <div key={card.label} className={className}>
            {body}
          </div>
        );
      })}
    </div>
  );
}
