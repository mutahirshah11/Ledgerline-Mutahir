import { expect, test } from "@playwright/test";

import {
  FILE_LIMITS,
  checkDrop,
  fileProblemText,
  type FileLike,
  type FileProblem,
} from "../../lib/file-rules";

// 006-web T030: lib/file-rules.ts, the SCR-03 drop rules checked before "Run" (SCR-03-AC2,
// FR-008; blueprint Section 7 / F-01.R3; limits as apps/api/app/services/stages.py).

const KB = 1024;
const MB = 1024 * KB;

const jpg = (name = "page.jpg", size = 400 * KB): FileLike => ({ name, size, type: "image/jpeg" });
const png = (name = "page.png", size = 400 * KB): FileLike => ({ name, size, type: "image/png" });
const csv = (name = "erp.csv", size = 3 * KB): FileLike => ({ name, size, type: "text/csv" });
const xlsx = (name = "sheet.xlsx", size = 6 * KB): FileLike => ({
  name,
  size,
  type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
});

const valid = () => ({ register: [jpg("p1.jpg"), png("p2.png")], erp: [csv()], sheet: [xlsx()] });

test("limits are 20 KB, 10 MB and 10 pages", () => {
  expect(FILE_LIMITS).toEqual({ minRegisterBytes: 20 * KB, maxBytes: 10 * MB, maxPages: 10 });
});

test("a valid drop has no problems and can run", () => {
  const c = checkDrop(valid());
  expect(c.register).toEqual({ files: [null, null], zone: null });
  expect(c.erp).toEqual({ files: [null], zone: null });
  expect(c.sheet).toEqual({ files: [null], zone: null });
  expect(c.canRun).toBe(true);
});

test("WRONG_TYPE: a register file that is not JPEG/PNG", () => {
  const c = checkDrop({ ...valid(), register: [jpg("p1.jpeg"), { name: "p2.gif", size: 50 * KB, type: "image/gif" }] });
  expect(c.register.files).toEqual([null, "WRONG_TYPE"]);
  expect(c.canRun).toBe(false);
});

test("WRONG_TYPE: upper-case extensions are fine", () => {
  const c = checkDrop({ ...valid(), register: [jpg("P1.JPG"), png("P2.PNG")] });
  expect(c.register.files).toEqual([null, null]);
});

test("TOO_SMALL: a register file under 20 KB (20 KB itself is fine)", () => {
  const c = checkDrop({ ...valid(), register: [jpg("a.jpg", 20 * KB - 1), jpg("b.jpg", 20 * KB)] });
  expect(c.register.files).toEqual(["TOO_SMALL", null]);
  expect(c.canRun).toBe(false);
});

test("TOO_LARGE: any file over 10 MB (10 MB itself is fine)", () => {
  const c = checkDrop({
    register: [jpg("a.jpg", 10 * MB + 1), jpg("b.jpg", 10 * MB)],
    erp: [csv("erp.csv", 10 * MB + 1)],
    sheet: [xlsx("s.xlsx", 10 * MB)],
  });
  expect(c.register.files).toEqual(["TOO_LARGE", null]);
  expect(c.erp.files).toEqual(["TOO_LARGE"]);
  expect(c.sheet.files).toEqual([null]);
  expect(c.canRun).toBe(false);
});

test("TOO_MANY_PAGES: the 11th page onwards is marked; 10 pages are fine", () => {
  const ten = Array.from({ length: 10 }, (_, i) => jpg(`p${i + 1}.jpg`));
  expect(checkDrop({ ...valid(), register: ten }).canRun).toBe(true);
  const twelve = Array.from({ length: 12 }, (_, i) => jpg(`p${i + 1}.jpg`));
  const c = checkDrop({ ...valid(), register: twelve });
  expect(c.register.files.slice(0, 10)).toEqual(Array(10).fill(null));
  expect(c.register.files.slice(10)).toEqual(["TOO_MANY_PAGES", "TOO_MANY_PAGES"]);
  expect(c.register.zone).toBe("TOO_MANY_PAGES");
  expect(c.canRun).toBe(false);
});

test("NO_REGISTER: zero register pages", () => {
  const c = checkDrop({ ...valid(), register: [] });
  expect(c.register).toEqual({ files: [], zone: "NO_REGISTER" });
  expect(c.canRun).toBe(false);
});

test("NEED_ONE_CSV: none, two, or a non-CSV in the ERP zone", () => {
  expect(checkDrop({ ...valid(), erp: [] }).erp).toEqual({ files: [], zone: "NEED_ONE_CSV" });
  const two = checkDrop({ ...valid(), erp: [csv("a.csv"), csv("b.csv")] });
  expect(two.erp.zone).toBe("NEED_ONE_CSV");
  expect(two.canRun).toBe(false);
  const wrong = checkDrop({ ...valid(), erp: [xlsx("erp.xlsx")] });
  expect(wrong.erp.files).toEqual(["NEED_ONE_CSV"]);
  expect(wrong.canRun).toBe(false);
});

test("NEED_ONE_XLSX: none, two, or a non-XLSX in the store sheet zone", () => {
  expect(checkDrop({ ...valid(), sheet: [] }).sheet).toEqual({ files: [], zone: "NEED_ONE_XLSX" });
  const two = checkDrop({ ...valid(), sheet: [xlsx("a.xlsx"), xlsx("b.xlsx")] });
  expect(two.sheet.zone).toBe("NEED_ONE_XLSX");
  const wrong = checkDrop({ ...valid(), sheet: [csv("sheet.csv")] });
  expect(wrong.sheet.files).toEqual(["NEED_ONE_XLSX"]);
  expect(wrong.canRun).toBe(false);
});

test("every FileProblem has its ui-copy.md §3 text", () => {
  const texts: Record<FileProblem, string> = {
    WRONG_TYPE: "Only JPEG or PNG photos",
    TOO_SMALL: "Photo is under 20 KB",
    TOO_LARGE: "Photo is over 10 MB",
    TOO_MANY_PAGES: "At most 10 register pages",
    NO_REGISTER: "Add at least one register page",
    NEED_ONE_CSV: "Add exactly one ERP CSV",
    NEED_ONE_XLSX: "Add exactly one store sheet (.xlsx)",
  };
  for (const [code, text] of Object.entries(texts)) {
    expect(fileProblemText(code as FileProblem)).toBe(text);
  }
});
