// 006-web T052: CSV export for SCR-05 (US5, SCR-05-AC5). RFC-4180-ish: fields containing a
// comma, double quote or newline are wrapped in double quotes with internal quotes doubled;
// rows are CRLF-joined. Callers pass only the rows they want written (the results page passes
// the currently filtered/visible rows, never the full unfiltered set) — this module does no
// filtering of its own.

export function csvField(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function csvRow(fields: string[]): string {
  return fields.map(csvField).join(",");
}

/** One visible SCR-05 results row (GRN, outcome, diff fields, outlier, each source's value). */
export type ResultsExportRow = {
  grn: string;
  outcome: string;
  item: string;
  supplier: string;
  /** "" when nothing differs; otherwise e.g. "Store sheet is different" or "Sources disagree". */
  outlier: string;
  /** "field: value; field: value" summary per source, for the compared fields that differ. */
  register: string;
  erp: string;
  sheet: string;
};

export const RESULTS_CSV_HEADERS = ["GRN", "Outcome", "Item", "Supplier", "Outlier", "Register", "ERP", "Store sheet"];

export function toResultsCsv(rows: ResultsExportRow[]): string {
  const lines = [
    csvRow(RESULTS_CSV_HEADERS),
    ...rows.map((r) => csvRow([r.grn, r.outcome, r.item, r.supplier, r.outlier, r.register, r.erp, r.sheet])),
  ];
  return lines.join("\r\n") + "\r\n";
}
