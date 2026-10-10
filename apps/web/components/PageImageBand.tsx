"use client";

// 006-web T046: the register photo with the reviewed row highlighted (research R10, SCR-06-AC2,
// AC3, AC8; contracts/components.md "PageImageBand").
// - Full-width image; the band is positioned in percent of the image height (lib/band.ts), so
//   it sits on the row at any width (380 px). The photo scrolls inside its own box so the band
//   is in view without moving the page.
// - Missing or invalid table edges: no band, "Row {n}" printed above the image.
// - The signed link lives 5 minutes: on the first load error the parent renews it once
//   (`onError` -> EP-16) and the image reloads; a second error shows a plain message.

import { useLayoutEffect, useRef, useState } from "react";

import { pct, rowBand } from "@/lib/band";

export type PageImageBandProps = {
  src: string | null;
  pageNo: number;
  /** The reviewed row (1..16); null for page-level items (no band, no row text). */
  rowIndex: number | null;
  tableTop: number | null | undefined;
  tableBottom: number | null | undefined;
  /** Renews the signed link once (EP-16) and resolves to the new URL. */
  onError: () => Promise<string>;
};

export function PageImageBand({ src, pageNo, rowIndex, tableTop, tableBottom, onError }: PageImageBandProps) {
  const [renewed, setRenewed] = useState<{ from: string | null; url: string } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const bandRef = useRef<HTMLDivElement>(null);
  const [loaded, setLoaded] = useState(false);

  // A renewed link belongs to the src it replaced; a new src (another page) starts over.
  const url = renewed && renewed.from === src ? renewed.url : src;
  const band = rowIndex === null ? null : rowBand(rowIndex, tableTop, tableBottom);

  useLayoutEffect(() => {
    const box = boxRef.current;
    const el = bandRef.current;
    if (!loaded || !box || !el) return;
    box.scrollTop = Math.max(0, el.offsetTop - (box.clientHeight - el.offsetHeight) / 2);
  }, [loaded, url]);

  async function handleError() {
    setLoaded(false);
    if (attempt > 0 || renewed?.from === src) {
      setFailed(true);
      return;
    }
    setAttempt(1);
    try {
      const next = await onError();
      setRenewed({ from: src, url: next });
    } catch {
      setFailed(true);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Register photo · Page {pageNo}
        </p>
        {rowIndex !== null && band === null ? (
          <p className="text-sm font-semibold text-foreground">Row {rowIndex}</p>
        ) : null}
      </div>
      {failed ? (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-900/30 dark:text-red-300">
          The page image could not be loaded.
        </p>
      ) : null}
      <div
        ref={boxRef}
        className="max-h-[65vh] w-full overflow-x-hidden overflow-y-auto rounded-xl border border-border bg-muted/20 shadow-sm"
      >
        <div className="relative w-full">
          {url ? (
            // eslint-disable-next-line @next/next/no-img-element -- short-lived signed link, not optimisable
            <img
              key={`${url}#${attempt}`}
              src={url}
              alt={`Register page ${pageNo}`}
              className="block h-auto w-full"
              onLoad={() => setLoaded(true)}
              onError={() => void handleError()}
            />
          ) : (
            <p className="p-6 text-sm text-muted-foreground">Loading…</p>
          )}
          {url && band ? (
            <div
              ref={bandRef}
              data-testid="row-band"
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 border-y-[3px] border-emerald-500 bg-emerald-400/20 shadow-[0_0_0_1px_rgba(16,185,129,0.25)]"
              style={{ top: pct(band.top), height: pct(band.height) }}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
