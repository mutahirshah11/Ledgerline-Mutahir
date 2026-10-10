// 006-web T054 (US5, SCR-05-AC4): plain-language sentences for EP-21 normalisation log entries.
//
// GAP: ui-copy.md has no table for `NormCode` (unlike the enums in its other sections). The one
// anchor is spec.md's SCR-05-AC4 worked example -- "Sheet date was text 9/14/2026, read as 14
// Sep" -- which this module reproduces using the canonical source label ("Store sheet", per
// ui-copy.md source table) rather than the spec prose's shorthand "Sheet": every other screen in
// this app spells the source out in full (DiffCell, OutcomeBadge's "Missing from Store sheet",
// ...), so SCR-05 keeps that convention rather than introducing a second, shorter label for the
// same source. Documented assumption (ambiguity noted for the coordinator): the sentence shapes
// for the other NormCode values are this task's own best-effort completeness, built on the AC4
// example's pattern ("{Source} {what changed}, read as {after}"), since nothing in the contracts
// fixes their exact wording.
import type { components } from "./api-types";
import { sourceLabel, type Source } from "./wording";

export type NormCode = components["schemas"]["NormCode"];
export type NormLogEntry = components["schemas"]["NormLogEntryOut"];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-14" -> "14 Sep" (day-of-month then short month, no year -- the log is one run's). */
function formatDayMonth(iso: string | null): string {
  if (!iso) return "unknown";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  const month = MONTHS[Number(m[2]) - 1];
  return month ? `${Number(m[3])} ${month}` : iso;
}

/** One `NormLogEntry` as a plain-language sentence for the given source (SCR-05-AC4). */
export function normLineText(entry: NormLogEntry, source: Source): string {
  const label = sourceLabel(source);
  const before = entry.before ?? "unknown";
  const after = entry.after ?? "unknown";
  switch (entry.code) {
    case "DATE_TEXT_PARSED":
      return `${label} date was text ${before}, read as ${formatDayMonth(entry.after)}`;
    case "DATE_SERIAL_PARSED":
      return `${label} date was a spreadsheet date number, read as ${formatDayMonth(entry.after)}`;
    case "DATE_YEAR_FROM_HEADER":
      return `${label} date had no year, read as ${formatDayMonth(entry.after)} (year taken from the page header)`;
    case "SUPPLIER_MAPPED":
      return `${label} supplier "${before}" matched to ${after}`;
    case "UNIT_MAPPED":
      return `${label} unit "${before}" matched to ${after}`;
    case "UNIT_CONVERTED":
      return `${label} quantity converted from ${before} to ${after}`;
    case "GRN_PREFIX_STRIPPED":
      return `${label} GRN number "${before}" read as ${after}`;
    case "TEXT_TRIMMED":
      return `${label} ${entry.field} had extra spaces removed`;
    default:
      return `${label} ${entry.field} was ${before}, read as ${after}`;
  }
}
