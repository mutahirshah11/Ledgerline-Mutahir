import { AlertTriangle, ArrowLeftRight, Clock3, Flag, Info } from "lucide-react";
import Link from "next/link";

import type { components } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { flagLabel } from "@/lib/wording";

// contracts/components.md "ExceptionSections" (006-web T040; F-09.R1, FR-016, FR-017, FR-018).
// Five headed sections from `run.exceptions` split by `category`, in the API's order; each line
// is the API's `text` verbatim (never composed here). A line about a receipt group links to that
// GRN on SCR-05; attached control flags (C5) show beside it in plain words. Empty sections are
// left out, except "Waiting on review" while review items are open.

export type ExceptionItem = components["schemas"]["ExceptionItem"];

export type ExceptionSectionsProps = {
  exceptions: ExceptionItem[];
  runId: string;
  /** EP-05 `open_review_items`: the "{n} items waiting" count (the review queue's size). */
  openReviewItems: number;
  /** FINALISED: no "Review now". */
  readOnly?: boolean;
};

/** ui-copy.md §4 section titles by F-09 category. */
export const SECTION_TITLES: Record<1 | 2 | 3 | 4 | 5, string> = {
  1: "Act on these",
  2: "Sources disagree",
  3: "Waiting on review",
  4: "Control flags",
  5: "Also",
};

const CATEGORIES = [1, 2, 3, 4, 5] as const;

/** Per-category icon (decorative, aria-hidden) and left-accent colour — organises the five
 * sections at a glance without relying on colour alone (NFR-09: the heading text always carries
 * the meaning; this is pure visual grouping). */
const SECTION_STYLE: Record<1 | 2 | 3 | 4 | 5, { icon: typeof AlertTriangle; accent: string }> = {
  1: { icon: AlertTriangle, accent: "border-l-red-400" },
  2: { icon: ArrowLeftRight, accent: "border-l-amber-400" },
  3: { icon: Clock3, accent: "border-l-blue-400" },
  4: { icon: Flag, accent: "border-l-orange-400" },
  5: { icon: Info, accent: "border-l-slate-300" },
};

function lineHref(runId: string, item: ExceptionItem): string | null {
  if (item.group_key && item.grn_no !== null) return `/runs/${runId}/results?grn=${item.grn_no}`;
  return null;
}

function ExceptionLine({ item, runId }: { item: ExceptionItem; runId: string }) {
  const href = lineHref(runId, item);
  return (
    <li className="py-2 text-sm [overflow-wrap:anywhere]">
      {href ? (
        <Link
          data-testid="exception-line"
          href={href}
          className="font-medium text-foreground underline-offset-4 hover:text-primary hover:underline"
        >
          {item.text}
        </Link>
      ) : (
        <span data-testid="exception-line">{item.text}</span>
      )}
      {item.flags.length > 0 && (
        <ul className="mt-1 flex flex-wrap gap-1" aria-label="Control flags on this line">
          {item.flags.map((f) => (
            <li
              key={f.id}
              className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-900 dark:bg-amber-900/30 dark:text-amber-300"
            >
              {flagLabel(f.flag)}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function ExceptionSections({ exceptions, runId, openReviewItems, readOnly = false }: ExceptionSectionsProps) {
  return (
    <>
      {CATEGORIES.map((category) => {
        const items = exceptions.filter((e) => e.category === category);
        const waiting = category === 3 && openReviewItems > 0;
        if (items.length === 0 && !waiting) return null;
        const headingId = `exceptions-${category}`;
        const { icon: Icon, accent } = SECTION_STYLE[category];
        return (
          <section
            key={category}
            aria-labelledby={headingId}
            className={cn("space-y-2 rounded-lg border-l-4 bg-card py-1 pl-4", accent)}
          >
            <h2 id={headingId} className="flex items-center gap-2 text-lg font-semibold">
              <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              {SECTION_TITLES[category]}
            </h2>
            {category === 3 && (
              <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                <span>{openReviewItems} items waiting</span>
                {!readOnly && openReviewItems > 0 && (
                  <Link href={`/runs/${runId}/review`} className="font-medium text-primary underline underline-offset-4">
                    Review now
                  </Link>
                )}
              </p>
            )}
            {items.length > 0 && (
              <ul className="divide-y">
                {items.map((item, i) => (
                  <ExceptionLine key={`${category}-${i}`} item={item} runId={runId} />
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </>
  );
}
