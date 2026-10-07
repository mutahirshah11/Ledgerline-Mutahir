// 006-web T046: the photo row band (research R10, SCR-06-AC3, blueprint Section 12).
//
// `table_top` / `table_bottom` are fractions of the image height that bound the 16 printed data
// rows only (Phase 0 clarification 1). Row `row_index` (1..16) is the slot
//   top = table_top + (row_index - 1) * h,  height = h,  h = (table_bottom - table_top) / 16
// as fractions of the rendered image height (so it works at any width). Missing or invalid
// edges (not 0 <= top < bottom <= 1) or a row outside 1..16 give no band: the screen prints
// "Row {row_index}" above the image instead.

/** Printed data-row slots on a register page (EP-16 `slots`). */
export const ROW_SLOTS = 16;

/** A band as fractions (0..1) of the rendered image height. */
export type Band = { top: number; height: number };

function isFraction(v: number | null | undefined): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1;
}

export function rowBand(
  rowIndex: number,
  tableTop: number | null | undefined,
  tableBottom: number | null | undefined,
  slots: number = ROW_SLOTS,
): Band | null {
  if (!Number.isInteger(rowIndex) || rowIndex < 1 || rowIndex > slots) return null;
  if (!isFraction(tableTop) || !isFraction(tableBottom) || tableTop >= tableBottom) return null;
  const height = (tableBottom - tableTop) / slots;
  return { top: tableTop + (rowIndex - 1) * height, height };
}

/** A fraction as a CSS percentage, rounded to 6 decimals ("79.4375%"). */
export function pct(fraction: number): string {
  return `${Number((fraction * 100).toFixed(6))}%`;
}
