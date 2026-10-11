import { ArrowRight, Flag } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { formatMonthLabel } from "@/lib/time";
import { runStatusLabel } from "@/lib/wording";
import type { components } from "@/lib/api-types";
import { Waffle, type WaffleCell } from "./Waffle";

export type RunView = components["schemas"]["RunView"];

export type DashboardHeroProps = {
  run: RunView;
  cells: WaffleCell[];
};

// Hero copy variants (README "Hero copy by state"): real numbers from the run, not invented
// per-screen copy — only the sentence template is fixed text.
function heroCopy(run: RunView) {
  const month = formatMonthLabel(run.month);
  const n = run.open_review_items;

  if (run.status === "FAILED") {
    return {
      chip: `${month} · failed`,
      headline: `${month} ${runStatusLabel(run.status, run.failed_stage).toLowerCase()}.`,
      body: run.error_message ?? "Something went wrong while this run was being built.",
      primaryLabel: `View ${month}`,
      primaryHref: `/runs/${run.id}`,
    };
  }
  if (run.status !== "RECONCILED" && run.status !== "AWAITING_REVIEW" && run.status !== "READY" && run.status !== "FINALISED") {
    return {
      chip: `${month} · in progress`,
      headline: `${month} is still being put together.`,
      body: `Ledgerline is ${runStatusLabel(run.status).toLowerCase()}. This usually only takes a few minutes.`,
      primaryLabel: `View ${month}`,
      primaryHref: `/runs/${run.id}`,
    };
  }
  if (run.status === "FINALISED") {
    return {
      chip: `${month} · closed`,
      headline: "You're all caught up.",
      body: `${month} is closed. Every receipt was reviewed and every row accounted for once.`,
      primaryLabel: `View ${month}`,
      primaryHref: `/runs/${run.id}`,
    };
  }
  if (n > 0) {
    return {
      chip: `${month} · your next step`,
      headline: `${month} is almost done. ${n} review item${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} you.`,
      body: run.headline,
      primaryLabel: `Review ${n} item${n === 1 ? "" : "s"}`,
      primaryHref: `/runs/${run.id}/review`,
    };
  }
  return {
    chip: `${month} · ready`,
    headline: `${month} is ready to close.`,
    body: run.headline,
    primaryLabel: `Close ${month}`,
    primaryHref: `/runs/${run.id}`,
  };
}

export function DashboardHero({ run, cells }: DashboardHeroProps) {
  const copy = heroCopy(run);
  return (
    <div className="flex flex-col gap-8 rounded-3xl bg-primary px-6 py-8 text-white shadow-[0_24px_48px_-28px_rgba(14,124,134,0.8)] sm:px-8 sm:py-9 lg:flex-row lg:items-center lg:gap-10">
      <div className="flex-1">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-white/15 px-3 py-1 text-xs font-medium">
          <Flag className="h-3.5 w-3.5" aria-hidden="true" />
          {copy.chip}
        </span>
        <h2 className="mt-4 font-heading text-3xl leading-tight font-bold tracking-tight text-balance sm:text-4xl">
          {copy.headline}
        </h2>
        <p className="mt-3 max-w-xl text-sm text-primary-tint-text sm:text-base">{copy.body}</p>
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <Button
            render={<Link href={copy.primaryHref} />}
            className="group gap-2 bg-white text-primary hover:bg-white/90"
          >
            {copy.primaryLabel}
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
          </Button>
          <Button
            render={<Link href={`/runs/${run.id}`} />}
            variant="outline"
            className="border-white/30 bg-transparent text-white hover:bg-white/10"
          >
            See what was checked
          </Button>
        </div>
      </div>
      {cells.length > 0 ? (
        <div className="flex-1 lg:max-w-sm">
          <Waffle cells={cells} />
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-primary-tint-text">
            <span className="inline-flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-[2px] bg-success" /> Matched
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-[2px] bg-warning" /> Needs attention
            </span>
            <span>Each square is one receipt (GRN)</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
