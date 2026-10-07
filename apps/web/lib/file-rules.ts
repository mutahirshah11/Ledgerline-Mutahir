// 006-web T032: the SCR-03 drop rules, checked in the browser before "Run" (SCR-03-AC2, FR-008;
// blueprint Section 7 / F-01.R3; data-model.md §2 DropState). The API checks the same rules again
// in EP-06 (apps/api/app/services/stages.py), which stays the authority.

export type FileProblem =
  | "WRONG_TYPE"
  | "TOO_SMALL"
  | "TOO_LARGE"
  | "TOO_MANY_PAGES"
  | "NO_REGISTER"
  | "NEED_ONE_CSV"
  | "NEED_ONE_XLSX";

/** The parts of a `File` the rules look at (a real `File` fits). */
export type FileLike = { name: string; size: number; type: string };

const KB = 1024;
const MB = 1024 * KB;

/** Same limits as the API: register photos 20 KB to 10 MB, any file at most 10 MB, 1–10 pages. */
export const FILE_LIMITS = { minRegisterBytes: 20 * KB, maxBytes: 10 * MB, maxPages: 10 } as const;

/** Problems of one zone: one entry per file (in list order) and one for the zone as a whole. */
export type ZoneCheck = { files: (FileProblem | null)[]; zone: FileProblem | null };

export type DropCheck = {
  register: ZoneCheck;
  erp: ZoneCheck;
  sheet: ZoneCheck;
  /** No problem anywhere: at least one register page, exactly one CSV and one XLSX. */
  canRun: boolean;
};

export type DropFiles = { register: FileLike[]; erp: FileLike[]; sheet: FileLike[] };

function ext(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

const REGISTER_EXT = new Set([".jpg", ".jpeg", ".png"]);

function registerProblem(f: FileLike, index: number): FileProblem | null {
  if (!REGISTER_EXT.has(ext(f.name))) return "WRONG_TYPE";
  if (f.size < FILE_LIMITS.minRegisterBytes) return "TOO_SMALL";
  if (f.size > FILE_LIMITS.maxBytes) return "TOO_LARGE";
  if (index >= FILE_LIMITS.maxPages) return "TOO_MANY_PAGES";
  return null;
}

function singleZone(files: FileLike[], extension: string, need: FileProblem): ZoneCheck {
  return {
    files: files.map((f) => {
      if (ext(f.name) !== extension) return need;
      if (f.size > FILE_LIMITS.maxBytes) return "TOO_LARGE";
      return null;
    }),
    zone: files.length === 1 ? null : need,
  };
}

export function checkDrop(drop: DropFiles): DropCheck {
  const register: ZoneCheck = {
    files: drop.register.map(registerProblem),
    zone:
      drop.register.length === 0
        ? "NO_REGISTER"
        : drop.register.length > FILE_LIMITS.maxPages
          ? "TOO_MANY_PAGES"
          : null,
  };
  const erp = singleZone(drop.erp, ".csv", "NEED_ONE_CSV");
  const sheet = singleZone(drop.sheet, ".xlsx", "NEED_ONE_XLSX");
  const clean = (z: ZoneCheck) => z.zone === null && z.files.every((p) => p === null);
  return { register, erp, sheet, canRun: clean(register) && clean(erp) && clean(sheet) };
}

/** contracts/ui-copy.md §3 "File problems". */
const PROBLEM_TEXT: Record<FileProblem, string> = {
  WRONG_TYPE: "Only JPEG or PNG photos",
  TOO_SMALL: "Photo is under 20 KB",
  TOO_LARGE: "Photo is over 10 MB",
  TOO_MANY_PAGES: "At most 10 register pages",
  NO_REGISTER: "Add at least one register page",
  NEED_ONE_CSV: "Add exactly one ERP CSV",
  NEED_ONE_XLSX: "Add exactly one store sheet (.xlsx)",
};

export function fileProblemText(problem: FileProblem): string {
  return PROBLEM_TEXT[problem];
}
