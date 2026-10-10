// 006-web T054 (US5, SCR-05): shapes EP-11's `GroupView[]` into what the results table, the
// expandable row and the CSV export need. One `DiffCell`-worth of recorded/compared/differs per
// source per group, built from the group's one primary `Diff` (FR-023/FR-024); groups with no
// diff (MATCHED, MATCHED_AFTER_CONVERSION) fall back to the row data itself, still showing a
// converted value beneath the recorded one when unit conversion changed it (SCR-05-AC2).
import type { DiffCellProps } from "@/components/DiffCell";

import type { components } from "./api-types";
import type { ResultsExportRow } from "./csv";
import { outcomeLabel, sourceLabel, type Source } from "./wording";

export type GroupView = components["schemas"]["GroupView"];
export type Group = components["schemas"]["Group"];
export type GroupRowView = components["schemas"]["GroupRowView"];
export type Diff = components["schemas"]["Diff"];
export type ComparedField = components["schemas"]["ComparedField"];

export const SOURCES: readonly Source[] = ["REGISTER", "ERP", "SHEET"];

/** The group's one headline diff (F-05.R6: a group has at most one differing field today). */
export function primaryDiff(group: Group): Diff | null {
  return group.diffs[0] ?? null;
}

/** The field this group's Register/ERP/Store sheet triplet should show: the diffing field when
 * there is one, else "qty" (the field every receipt has and the one SCR-05-AC2's conversion
 * example is about). */
export function groupDisplayField(gv: GroupView): ComparedField {
  return primaryDiff(gv.group)?.field ?? "qty";
}

function rowFor(gv: GroupView, source: Source): GroupRowView | undefined {
  return gv.rows[source]?.[0];
}

function fieldDisplay(row: GroupRowView, field: ComparedField): string | null {
  if (field === "qty") return row.qty !== null ? `${row.qty} ${row.uom ?? ""}`.trim() : null;
  if (field === "date") return row.date;
  if (field === "supplier_code") return row.supplier_code;
  if (field === "lot_no") return row.lot_no;
  return row.uom;
}

function fieldCompared(row: GroupRowView, field: ComparedField): string | null {
  if (field !== "qty" || row.qty_base === null) return null;
  const sameValue = row.qty_base === row.qty && (row.base_uom ?? null) === (row.uom ?? null);
  return sameValue ? null : `${row.qty_base} ${row.base_uom ?? ""}`.trim();
}

/** `Record<Source, DiffCellProps>` for `SourceTriplet`/`DiffCell` on one group's one field. */
export function cellsFor(gv: GroupView, field: ComparedField): Record<Source, DiffCellProps> {
  const diff = gv.group.diffs.find((d) => d.field === field) ?? null;
  const result = {} as Record<Source, DiffCellProps>;
  for (const source of SOURCES) {
    const label = sourceLabel(source);
    if (gv.group.missing_from.includes(source)) {
      result[source] = { recorded: null, compared: null, differs: false, isOutlier: false, absent: true, sourceLabel: label };
      continue;
    }
    if (diff) {
      result[source] = {
        recorded: diff.values[source] ?? null,
        compared: diff.compared[source] ?? null,
        differs: true,
        isOutlier: diff.outlier === source,
        sourceLabel: label,
      };
      continue;
    }
    const row = rowFor(gv, source);
    result[source] = {
      recorded: row ? fieldDisplay(row, field) : null,
      compared: row ? fieldCompared(row, field) : null,
      differs: false,
      isOutlier: false,
      sourceLabel: label,
    };
  }
  return result;
}

/** The item description shown on the row: the first source that has one (they should agree). */
export function itemOf(gv: GroupView): string {
  for (const source of SOURCES) {
    const row = rowFor(gv, source);
    if (row?.item) return row.item;
  }
  return "";
}

/** The supplier code shown on the row. */
export function supplierOf(gv: GroupView): string {
  for (const source of SOURCES) {
    const row = rowFor(gv, source);
    if (row?.supplier_code) return row.supplier_code;
  }
  return "";
}

/** One `ResultsExportRow` (lib/csv.ts) for a group, in the order the group's own (possibly
 * filtered) list is shown -- the page passes only the currently visible groups. */
export function toExportRow(gv: GroupView): ResultsExportRow {
  const field = groupDisplayField(gv);
  const diff = primaryDiff(gv.group);
  const cells = cellsFor(gv, field);
  const outlierText = diff?.outlier
    ? outcomeLabel(gv.group.outcome, { outlier: diff.outlier })
    : gv.group.missing_from.length > 0
      ? outcomeLabel(gv.group.outcome, { missingFrom: gv.group.missing_from })
      : "";
  const perSource = (source: Source) =>
    cells[source].differs && cells[source].recorded !== null ? `${field}: ${cells[source].recorded}` : "";
  return {
    grn: gv.group.grn_no !== null ? String(gv.group.grn_no) : gv.group.group_key,
    outcome: outcomeLabel(gv.group.outcome),
    item: itemOf(gv),
    supplier: supplierOf(gv),
    outlier: outlierText,
    register: perSource("REGISTER"),
    erp: perSource("ERP"),
    sheet: perSource("SHEET"),
  };
}
