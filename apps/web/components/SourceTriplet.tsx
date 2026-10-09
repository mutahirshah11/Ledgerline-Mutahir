import { DiffCell, type DiffCellProps } from "@/components/DiffCell";
import type { ComparedField } from "@/lib/results";
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

export function SourceTriplet({ field, cells }: SourceTripletProps) {
  return (
    <div className="flex flex-col gap-2 sm:grid sm:grid-cols-3" data-field={field}>
      {ORDER.map((source) => (
        <div key={source} className="flex min-w-0 flex-col gap-0.5">
          <span className="text-xs font-medium text-muted-foreground">{cells[source].sourceLabel}</span>
          <DiffCell {...cells[source]} />
        </div>
      ))}
    </div>
  );
}
