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
