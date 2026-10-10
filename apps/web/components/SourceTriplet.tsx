import { BookText, Database, FileSpreadsheet } from "lucide-react";
import type { ComponentType } from "react";

import { DiffCell, type DiffCellProps } from "@/components/DiffCell";
import type { ComparedField } from "@/lib/results";
import { cn } from "@/lib/utils";
import type { Source } from "@/lib/wording";

// contracts/components.md "SourceTriplet" (006-web T053; US5, SCR-05-AC1/AC2). Always REGISTER /
// ERP / SHEET in that order; three columns on normal screens, stacked (each with its own label)
// below 380px (NFR-07). Each `DiffCellProps` already carries its own `sourceLabel` for the a11y
// text; the visible caption here is the same label, shown for sighted users too.

export type SourceTripletProps = {
  field: ComparedField;
  cells: Record<Source, DiffCellProps>;
};

const ORDER: readonly Source[] = ["REGISTER", "ERP", "SHEET"];

const SOURCE_ICON: Record<Source, ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>> = {
  REGISTER: BookText,
  ERP: Database,
  SHEET: FileSpreadsheet,
};

export function SourceTriplet({ field, cells }: SourceTripletProps) {
  return (
    <div
      className="flex flex-col gap-3 sm:grid sm:grid-cols-3 sm:gap-0 sm:divide-x sm:divide-border/60"
      data-field={field}
    >
      {ORDER.map((source, i) => {
        const Icon = SOURCE_ICON[source];
        return (
          <div key={source} className={cn("flex min-w-0 flex-col gap-0.5", i > 0 && "sm:pl-3", i < ORDER.length - 1 && "sm:pr-3")}>
            <span className="inline-flex items-center gap-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
              <Icon className="size-3" aria-hidden="true" />
              {cells[source].sourceLabel}
            </span>
            <DiffCell {...cells[source]} />
          </div>
        );
      })}
    </div>
  );
}
