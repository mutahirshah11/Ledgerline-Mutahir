import { expect, test } from "@playwright/test";

import { formatDateTime, formatTime } from "../../lib/time";

// lib/time.ts: API times (UTC from Supabase, or +05:00 from the scenario clock) shown in the
// business time zone, Asia/Karachi, 24-hour.

test("formatTime shows Karachi wall-clock HH:MM for Z, +00:00 and +05:00 inputs", () => {
  expect(formatTime("2026-10-15T03:31:30Z")).toBe("08:31");
  expect(formatTime("2026-10-15T03:31:30+00:00")).toBe("08:31");
  expect(formatTime("2026-10-15T08:31:30+05:00")).toBe("08:31");
  expect(formatTime("2026-10-14T19:05:00Z")).toBe("00:05");
});

test("formatDateTime shows 'D Mon YYYY, HH:MM' in Karachi time", () => {
  expect(formatDateTime("2026-10-15T03:31:30Z")).toBe("15 Oct 2026, 08:31");
  expect(formatDateTime("2026-10-15T03:31:30.123456+00:00")).toBe("15 Oct 2026, 08:31");
  expect(formatDateTime("2026-10-15T08:31:30+05:00")).toBe("15 Oct 2026, 08:31");
  expect(formatDateTime("2026-10-14T19:05:00Z")).toBe("15 Oct 2026, 00:05");
});
