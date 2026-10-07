import { expect, test } from "@playwright/test";

import { csvField, RESULTS_CSV_HEADERS, toResultsCsv, type ResultsExportRow } from "../../lib/csv";

// 006-web T050: lib/csv.ts unit tests (US5, SCR-05-AC5). CSV uses CRLF line endings and
// RFC-4180-style quoting; only rows the caller passes in appear (the results page passes the
// currently visible/filtered rows only, never the full unfiltered set).

test("csvField leaves plain values alone", () => {
  expect(csvField("4425")).toBe("4425");
  expect(csvField("MISMATCHED")).toBe("MISMATCHED");
});

test('csvField quotes and escapes a value containing a double quote (GREY FABRIC 63")', () => {
  expect(csvField('GREY FABRIC 63"')).toBe('"GREY FABRIC 63"""');
});

test("csvField quotes a value containing a comma or newline", () => {
  expect(csvField("a,b")).toBe('"a,b"');
  expect(csvField("a\nb")).toBe('"a\nb"');
});

test("toResultsCsv writes the header row first, then one row per visible result, CRLF-joined", () => {
  const rows: ResultsExportRow[] = [
    {
      grn: "4425",
      outcome: "All three differ",
      item: 'GREY FABRIC 63"',
      supplier: "S-107",
      outlier: "Store sheet is different",
      register: "qty: 1200 YD",
      erp: "qty: 1097.28 M",
      sheet: "qty: 1200 M",
    },
  ];
  const csv = toResultsCsv(rows);
  const lines = csv.split("\r\n");
  expect(lines[0]).toBe(RESULTS_CSV_HEADERS.join(","));
  expect(lines[1]).toBe(
    ['4425', 'All three differ', '"GREY FABRIC 63"""', 'S-107', 'Store sheet is different', 'qty: 1200 YD', 'qty: 1097.28 M', 'qty: 1200 M'].join(","),
  );
  expect(lines[lines.length - 1]).toBe(""); // trailing CRLF
});

test("toResultsCsv only includes the rows passed in (visible rows only, no hidden filtering inside)", () => {
  const rows: ResultsExportRow[] = [
    { grn: "1", outcome: "o", item: "i", supplier: "s", outlier: "", register: "", erp: "", sheet: "" },
  ];
  expect(toResultsCsv(rows).split("\r\n").filter(Boolean)).toHaveLength(2); // header + 1 row
});
