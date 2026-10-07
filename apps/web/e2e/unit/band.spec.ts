import { expect, test } from "@playwright/test";

import { pct, rowBand } from "../../lib/band";

// 006-web T044: lib/band.ts (research R10, SCR-06-AC3): the highlighted band over register row
// `row_index` of the 16 printed slots between the table edges (fractions of the image height).

test("R10 band: row 14 of 16 between 0.12 and 0.95 is top 79.4375%, height 5.1875%", () => {
  const band = rowBand(14, 0.12, 0.95);
  expect(band).not.toBeNull();
  expect(band!.top).toBeCloseTo(0.794375, 12);
  expect(band!.height).toBeCloseTo(0.051875, 12);
  expect(pct(band!.top)).toBe("79.4375%");
  expect(pct(band!.height)).toBe("5.1875%");
});

test("R10 band: row 1 starts at the top edge and row 16 ends at the bottom edge", () => {
  expect(pct(rowBand(1, 0.199, 0.964)!.top)).toBe("19.9%");
  const last = rowBand(16, 0.199, 0.964)!;
  expect(last.top + last.height).toBeCloseTo(0.964, 12);
  expect(pct(rowBand(14, 0.199, 0.964)!.top)).toBe("82.05625%");
});

test("R10 band: missing edges give no band", () => {
  expect(rowBand(14, null, 0.95)).toBeNull();
  expect(rowBand(14, 0.12, null)).toBeNull();
  expect(rowBand(14, undefined, undefined)).toBeNull();
});

test("R10 band: invalid edges or row give no band", () => {
  expect(rowBand(14, 0.95, 0.12)).toBeNull(); // top after bottom
  expect(rowBand(14, 0.5, 0.5)).toBeNull(); // empty table
  expect(rowBand(14, -0.1, 0.9)).toBeNull();
  expect(rowBand(14, 0.1, 1.2)).toBeNull();
  expect(rowBand(14, Number.NaN, 0.9)).toBeNull();
  expect(rowBand(0, 0.12, 0.95)).toBeNull();
  expect(rowBand(17, 0.12, 0.95)).toBeNull();
  expect(rowBand(2.5, 0.12, 0.95)).toBeNull();
  expect(rowBand(1, 0, 1)).not.toBeNull(); // the whole image is a valid table
});
