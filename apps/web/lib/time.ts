// Times on screen (006-web): the API sends ISO instants (UTC "Z"/"+00:00" from Supabase, or the
// scenario clock's "+05:00"); the app always shows them in the business time zone, Asia/Karachi,
// 24-hour, the same on the server and in any browser.

export const BUSINESS_TIME_ZONE = "Asia/Karachi";

const TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: BUSINESS_TIME_ZONE,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: BUSINESS_TIME_ZONE,
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function parts(fmt: Intl.DateTimeFormat, iso: string): Record<string, string> | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return Object.fromEntries(fmt.formatToParts(d).map((p) => [p.type, p.value]));
}

/** "08:31" (Asia/Karachi). Unparseable input is returned as received. */
export function formatTime(iso: string): string {
  const p = parts(TIME, iso);
  return p ? `${p.hour}:${p.minute}` : iso;
}

/** "15 Oct 2026, 08:31" (Asia/Karachi). Unparseable input is returned as received. */
export function formatDateTime(iso: string): string {
  const p = parts(DATE_TIME, iso);
  return p ? `${p.day} ${p.month} ${p.year}, ${p.hour}:${p.minute}` : iso;
}

const MONTH_LONG = new Intl.DateTimeFormat("en-GB", {
  timeZone: BUSINESS_TIME_ZONE,
  month: "long",
  year: "numeric",
});
const MONTH_SHORT_DATE = new Intl.DateTimeFormat("en-GB", {
  timeZone: BUSINESS_TIME_ZONE,
  day: "numeric",
  month: "short",
});

/** "YYYY-MM" -> "September 2026". Unparseable input is returned as received. */
export function formatMonthLabel(month: string): string {
  const d = new Date(`${month}-01T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return month;
  return MONTH_LONG.format(d);
}

/** "YYYY-MM" -> "SEP". Unparseable input is returned as received. */
export function formatMonthShort(month: string): string {
  const d = new Date(`${month}-01T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return month;
  return d.toLocaleString("en-GB", { month: "short", timeZone: BUSINESS_TIME_ZONE }).toUpperCase();
}

/**
 * Relative time for recent instants ("Just now", "Yesterday", "3 days ago"), falling back to a
 * short date ("Sep 6") beyond a week — display only (UI copy), never used for business logic.
 */
export function formatRelative(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.round(diffMs / 60_000);
  if (diffMin < 1) return "Just now";
  if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? "" : "s"} ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour} hour${diffHour === 1 ? "" : "s"} ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay === 1) return "Yesterday";
  if (diffDay < 7) return `${diffDay} days ago`;
  const p = parts(MONTH_SHORT_DATE, iso);
  return p ? `${p.month} ${p.day}` : iso;
}
