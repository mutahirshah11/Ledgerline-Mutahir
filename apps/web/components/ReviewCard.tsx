"use client";

// 006-web T047: one review item on SCR-06 (contracts/components.md "ReviewCard", data-model
// §2 ReviewCardModel and §3.2, ui-copy.md §5; FR-026..FR-028, F-08.R3/R4, WEB-AC1).
//
// Order: reason line, photo with row band (items on a register page), Read A / Read B,
// "other sources, for reference", value input (always empty, F-08.R3) and note, actions.
// Actions are exactly those EP-15 allows for the item's reason (apps/api app/services/review.py
// ALLOWED_ACTIONS: the contract wins over the data-model table, which lacks Enter value for
// UNMATCHED_NO_KEY and Accept read for ROW_IN_ONE_READ / PAGE_LABEL_MISMATCH). Values are checked
// with the API's own rules before sending (qty ASCII digits with an optional fraction, date
// DD/MM, lot L####-###, GRN 3 to 6 digits); they stay strings. The note is kept in
// sessionStorage per item id until the item is decided, so a 401 sign-in round trip keeps it.

import { AlertTriangle, CheckCircle2, HelpCircle, Loader2, PencilLine, ThumbsUp } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { PageImageBand } from "@/components/PageImageBand";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import type { components } from "@/lib/api-types";
import { cellStatusLabel, flagLabel, reviewReasonLabel, sourceLabel } from "@/lib/wording";

type S = components["schemas"];
export type ApiReviewCard = S["ReviewCard"];
export type ApiReviewItem = S["ReviewItem"];
export type PageImageLink = S["PageImageLink"];
export type ReviewAction = S["ReviewAction"];
export type ReviewKind = S["ReviewKind"];
export type ReviewReason = S["ReviewReason"];
export type Cell = S["Cell"];

export type ReviewDecision = { value: string | null; by: string | null; at: string | null; note: string | null };

/** data-model §2 ReviewCardModel (EP-14 card + its page's EP-16 link). */
export type ReviewCardModel = {
  id: string;
  kind: ReviewKind;
  reason: ReviewReason;
  grnNo: number | null;
  sourceRowId: string | null;
  field: string | null;
  reasonLine: string;
  readA: Cell | null;
  readB: Cell | null;
  acceptable: boolean;
  actions: ReviewAction[];
  /** The field an entered value is checked as (qty, date, lot_no, unit, supplier, grn_no). */
  valueField: string | null;
  image: {
    pageNo: number;
    rowIndex: number | null;
    tableTop: number | null;
    tableBottom: number | null;
    signedUrl: string | null;
  } | null;
  otherSources: { label: string; values: { source: "ERP" | "SHEET"; value: string | null }[] } | null;
  /** Plain "label: value" lines for items that are not a single cell (flags, rows, labels). */
  details: { label: string; value: string }[];
  status: S["ReviewStatus"];
  decision: ReviewDecision | null;
};

export type ResolveInput = { action: ReviewAction; value?: string; note?: string };

export type ReviewCardProps = {
  item: ReviewCardModel;
  readOnly: boolean;
  readOnlyReason?: string;
  onAction: (a: ResolveInput) => Promise<void>;
  onImageError: () => Promise<string>;
};

// --- model --------------------------------------------------------------------------------

const ACTION_LABEL: Record<ReviewAction, string> = {
  ACCEPT_READ: "Accept read",
  ENTER_VALUE: "Enter value",
  CANNOT_DETERMINE: "Cannot determine",
  ACKNOWLEDGE: "Acknowledge",
};
const ACTION_ICON: Record<ReviewAction, typeof CheckCircle2> = {
  ACCEPT_READ: CheckCircle2,
  ENTER_VALUE: PencilLine,
  CANNOT_DETERMINE: HelpCircle,
  ACKNOWLEDGE: ThumbsUp,
};
const ACTION_ORDER: ReviewAction[] = ["ACCEPT_READ", "ENTER_VALUE", "CANNOT_DETERMINE", "ACKNOWLEDGE"];

const CELL_ACTIONS: ReviewAction[] = ["ACCEPT_READ", "ENTER_VALUE", "CANNOT_DETERMINE"];
const ALLOWED_ACTIONS: Record<ReviewReason, ReviewAction[]> = {
  ILLEGIBLE: CELL_ACTIONS,
  CORRECTED: CELL_ACTIONS,
  UNCLEAR: CELL_ACTIONS,
  READS_DISAGREE: CELL_ACTIONS,
  ROW_IN_ONE_READ: ["ACCEPT_READ", "CANNOT_DETERMINE"],
  PAGE_LABEL_MISMATCH: ["ACCEPT_READ", "CANNOT_DETERMINE"],
  UNKNOWN_SUPPLIER: ["ENTER_VALUE", "CANNOT_DETERMINE"],
  UNKNOWN_UNIT: ["ENTER_VALUE", "CANNOT_DETERMINE"],
  UNPARSEABLE_DATE: ["ENTER_VALUE", "CANNOT_DETERMINE"],
  UNMATCHED_NO_KEY: ["ENTER_VALUE", "CANNOT_DETERMINE"],
  CONFLICTING_ROWS_IN_SOURCE: ["CANNOT_DETERMINE"],
  SEQUENCE_GAP: ["ACKNOWLEDGE"],
  BACKDATED_ENTRY: ["ACKNOWLEDGE"],
  DATE_OUT_OF_PERIOD: ["ACKNOWLEDGE"],
};

const FIELD_LABEL: Record<string, string> = {
  qty: "Qty",
  unit: "Unit",
  supplier: "Supplier",
  lot_no: "Lot",
  date: "Date",
  grn_no: "GRN",
  item: "Item",
  recd_by: "Received by",
};

function fieldLabel(field: string): string {
  return FIELD_LABEL[field] ?? capitalise(field.replace(/_/g, " "));
}

function capitalise(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** A read's value for the reason line: the value, else its status ("unreadable", "unclear"). */
function readShort(cell: Cell | null): string {
  if (!cell) return "none";
  return cell.value ?? cellStatusLabel(cell.status);
}

/** A read in full for the reads block (ui-copy §5 "{struck} struck out, {current} written"). */
export function readText(cell: Cell | null): string {
  if (!cell) return "no read";
  if (cell.status === "CORRECTED" && cell.struck != null && cell.value != null) {
    return `${cell.struck} struck out, ${cell.value} written`;
  }
  if (cell.status === "UNCLEAR") {
    const c = cell.candidates ?? [];
    return c.length ? `unclear: ${c.join(" or ")}` : "unclear";
  }
  if (cell.status === "ILLEGIBLE") return "unreadable";
  return cell.value ?? "no value";
}

const ACCEPTABLE = new Set<Cell["status"]>(["CLEAR", "CORRECTED"]);

function same(a: string | null | undefined, b: string | null | undefined): boolean {
  return a != null && b != null && a.trim().toLowerCase() === b.trim().toLowerCase();
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

type Ctx = ApiReviewCard["context"];
const has = <K extends string>(ctx: Ctx, key: K): ctx is Extract<Ctx, Record<K, unknown>> =>
  isRecord(ctx) && key in ctx;

/** Accept read is possible when both reads give one value (CLEAR or a CORRECTED current
 * value, equal after trim and case-fold); a one-read row is kept as read; a page label needs
 * both reads to write the same page number (apps/api accept_read_value). */
function acceptableOf(card: ApiReviewCard): boolean {
  if (card.reason === "ROW_IN_ONE_READ") return true;
  const ctx = card.context;
  if (card.reason === "PAGE_LABEL_MISMATCH" && has(ctx, "page_no_written")) {
    return same(ctx.page_no_written.A, ctx.page_no_written.B);
  }
  const a = card.read_a;
  const b = card.read_b;
  return !!a && !!b && ACCEPTABLE.has(a.status) && ACCEPTABLE.has(b.status) && same(a.value, b.value);
}

function valueFieldOf(card: ApiReviewCard): string | null {
  if (card.reason === "UNMATCHED_NO_KEY") return "grn_no";
  return card.field;
}

function reasonLineOf(card: ApiReviewCard): string {
  const grn = card.grn_no != null ? String(card.grn_no) : "No GRN";
  const ctx = card.context;
  const ctxNote = "note" in ctx && typeof ctx.note === "string" ? ctx.note : null;
  if (card.kind === "CELL" && card.field && card.reason !== "ROW_IN_ONE_READ" && card.reason !== "PAGE_LABEL_MISMATCH") {
    const why = capitalise(ctxNote?.trim() || reviewReasonLabel(card.reason));
    return `${grn} · ${fieldLabel(card.field)} · ${why}. Read A: ${readShort(card.read_a)}. Read B: ${readShort(card.read_b)}.`;
  }
  const parts = [grn];
  if (card.field) parts.push(fieldLabel(card.field));
  parts.push(capitalise(reviewReasonLabel(card.reason)));
  return parts.join(" · ");
}

function detailsOf(card: ApiReviewCard): { label: string; value: string }[] {
  const ctx = card.context;
  if (has(ctx, "flag")) {
    return [
      { label: "Flag", value: flagLabel(ctx.flag) },
      ...Object.entries(ctx.detail).map(([k, v]) => ({ label: capitalise(k.replace(/_/g, " ")), value: v })),
    ];
  }
  if (has(ctx, "raw") && has(ctx, "source")) {
    return [{ label: `As written in ${sourceLabel(ctx.source)}`, value: ctx.raw }];
  }
  if (has(ctx, "group_key")) {
    return [
      { label: "Group", value: ctx.group_key },
      ...(ctx.other_rows.length ? [{ label: "Other rows", value: ctx.other_rows.join(", ") }] : []),
    ];
  }
  if (has(ctx, "page_no_written")) {
    return [
      { label: "Run month", value: ctx.run_month },
      { label: "Page number written (Read A / Read B)", value: `${ctx.page_no_written.A ?? "none"} / ${ctx.page_no_written.B ?? "none"}` },
      { label: "Month written (Read A / Read B)", value: `${ctx.month_written.A ?? "none"} / ${ctx.month_written.B ?? "none"}` },
    ];
  }
  if (has(ctx, "cells") && has(ctx, "read")) {
    return [
      { label: "Seen only in", value: `Read ${ctx.read}` },
      ...Object.entries(ctx.cells).map(([f, c]) => ({ label: fieldLabel(f), value: readText(c) })),
    ];
  }
  return [];
}

/** EP-14 card (+ the EP-16 link of its page, when loaded) -> the card's view model. */
export function toReviewCardModel(card: ApiReviewCard, link: PageImageLink | null): ReviewCardModel {
  const ctx = card.context;
  let image: ReviewCardModel["image"] = null;
  if (has(ctx, "page_no")) {
    const rowIndex = has(ctx, "row_index") ? ctx.row_index : null;
    const ctxTop = has(ctx, "table_top") ? ctx.table_top : null;
    const ctxBottom = has(ctx, "table_bottom") ? ctx.table_bottom : null;
    image = {
      pageNo: ctx.page_no,
      rowIndex,
      // EP-16 carries read A's table edges for the page; the card context has the same edges.
      tableTop: link ? link.table_top : ctxTop,
      tableBottom: link ? link.table_bottom : ctxBottom,
      signedUrl: link?.signed_url ?? null,
    };
  }
  let otherSources: ReviewCardModel["otherSources"] = null;
  if (has(ctx, "other_sources")) {
    otherSources = {
      label: ctx.other_sources_label || "other sources, for reference",
      values: (["ERP", "SHEET"] as const).map((source) => ({ source, value: ctx.other_sources[source] ?? null })),
    };
  }
  return {
    id: card.id,
    kind: card.kind,
    reason: card.reason,
    grnNo: card.grn_no,
    sourceRowId: card.source_row_id,
    field: card.field,
    reasonLine: reasonLineOf(card),
    readA: card.read_a,
    readB: card.read_b,
    acceptable: acceptableOf(card),
    actions: ALLOWED_ACTIONS[card.reason] ?? [],
    valueField: valueFieldOf(card),
    image,
    otherSources,
    details: detailsOf(card),
    status: card.status,
    decision:
      card.status === "OPEN"
        ? null
        : { value: card.resolved_value, by: card.resolved_by, at: card.resolved_at, note: card.note },
  };
}

// --- local checks (the API re-checks, F-08.R4) ---------------------------------------------

const QTY = /^[0-9]+(\.[0-9]+)?$/; // ASCII digits only, no sign, no exponent (apps/api _QTY)
const DATE = /^[0-9]{2}\/[0-9]{2}$/;
const LOT = /^L[0-9]{4}-[0-9]{3}$/;
const GRN = /^[0-9]{3,6}$/;

/** The API's wording for a value it would refuse, or null when the value may be sent. */
export function checkValue(field: string | null, raw: string): string | null {
  const text = raw.trim();
  if (!text) return "A value is required";
  if (text.length > 64) return "A value can be at most 64 characters";
  switch (field) {
    case "qty":
      return QTY.test(text) ? null : "qty must be a number of 0 or more";
    case "date":
      return DATE.test(text) ? null : "date must be DD/MM";
    case "lot_no":
      return LOT.test(text.toUpperCase()) ? null : "lot must look like L2609-128";
    case "grn_no":
      return GRN.test(text) && /[1-9]/.test(text) ? null : "GRN must be 3 to 6 digits";
    default:
      return null; // unit / supplier: the mapping files decide (API 422)
  }
}

// --- note kept for the session (WEB-AC1) ----------------------------------------------------

const noteKey = (id: string) => `ledgerline.review-note.${id}`;

function readNote(id: string): string {
  try {
    return window.sessionStorage.getItem(noteKey(id)) ?? "";
  } catch {
    return "";
  }
}

function writeNote(id: string, note: string): void {
  try {
    if (note) window.sessionStorage.setItem(noteKey(id), note);
    else window.sessionStorage.removeItem(noteKey(id));
  } catch {
    // storage unavailable: the note just isn't kept across a sign-in
  }
}

/** Forget an item's kept note (after it is decided). */
export function clearKeptNote(id: string): void {
  writeNote(id, "");
}

// --- errors --------------------------------------------------------------------------------

const ISO_MINUTE = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/;

/** "2026-10-15T08:31:15+05:00" -> "2026-10-15 08:31" (the API's own local time, no clock). */
function shortTime(iso: string | null): string {
  if (!iso) return "an unknown time";
  const m = iso.match(ISO_MINUTE);
  return m ? `${m[1]} ${m[2]}` : iso;
}

export function conflictText(item: Partial<ApiReviewItem> | null): string {
  const value =
    item?.resolved_value ?? (item?.status === "CANNOT_DETERMINE" ? "Cannot determine" : "Acknowledged");
  return `Already decided by ${item?.resolved_by ?? "another reviewer"} at ${shortTime(item?.resolved_at ?? null)}: ${value}`;
}

type CardError = { kind: "error" | "conflict"; text: string };

function errorOf(err: unknown): CardError | null {
  if (!(err instanceof ApiError)) return { kind: "error", text: "Ledgerline is not reachable right now" };
  if (err.status === 401) return null; // on the way to sign-in; the note is kept
  if (err.status === 409 && err.code === "ITEM_ALREADY_RESOLVED") {
    const item = isRecord(err.details.item) ? (err.details.item as Partial<ApiReviewItem>) : null;
    return { kind: "conflict", text: conflictText(item) };
  }
  if (err.status === 409 && err.code === "RUN_STATE_CONFLICT") {
    return { kind: "error", text: "This run is finalised and read-only" };
  }
  if (err.status === 422) {
    const reason = typeof err.details.reason === "string" ? err.details.reason : null;
    return { kind: "error", text: reason ? `${err.message}: ${reason}` : err.message };
  }
  if (err.status === 403) return { kind: "error", text: "Your role cannot do this" };
  if (err.status === 0 || err.status >= 500) return { kind: "error", text: "Ledgerline is not reachable right now" };
  return { kind: "error", text: err.message };
}

// --- view ----------------------------------------------------------------------------------

function ReadBlock({ label, cell, testId }: { label: string; cell: Cell | null; testId: string }) {
  return (
    <div data-testid={testId} className="min-w-0 rounded-xl border border-border/70 bg-muted/30 p-3">
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-[10px] font-bold text-white"
        >
          {label.slice(-1)}
        </span>
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</p>
      </div>
      <p className="mt-1.5 break-words text-base font-semibold text-foreground">{readText(cell)}</p>
      {cell ? <p className="mt-0.5 text-xs text-muted-foreground">{cellStatusLabel(cell.status)}</p> : null}
      {cell?.note ? <p className="mt-1 break-words text-sm text-muted-foreground">{cell.note}</p> : null}
    </div>
  );
}

export function ReviewCard({ item, readOnly, readOnlyReason, onAction, onImageError }: ReviewCardProps) {
  const ids = useId();
  const [value, setValue] = useState(""); // never pre-filled (F-08.R3)
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<CardError | null>(null);

  // Restore a note typed before a sign-in round trip (after mount: sessionStorage is browser-only).
  useEffect(() => {
    const kept = readNote(item.id);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time restore from browser storage
    if (kept) setNote(kept);
  }, [item.id]);

  const conflict = error?.kind === "conflict";
  const decided = item.decision !== null;
  const showActions = !readOnly && !conflict && !decided;
  const canEnter = item.actions.includes("ENTER_VALUE");

  async function act(action: ReviewAction) {
    const trimmedNote = note.trim();
    if (action === "ENTER_VALUE") {
      const problem = checkValue(item.valueField, value);
      if (problem) return setError({ kind: "error", text: problem });
    }
    if (action === "CANNOT_DETERMINE" && !trimmedNote) {
      return setError({ kind: "error", text: "A note is required" });
    }
    if (trimmedNote.length > 500) return setError({ kind: "error", text: "A note can be at most 500 characters" });
    const body: ResolveInput = { action };
    if (action === "ENTER_VALUE") body.value = value.trim();
    if (trimmedNote) body.note = trimmedNote;
    setBusy(true);
    setError(null);
    try {
      await onAction(body);
      clearKeptNote(item.id);
    } catch (err) {
      setError(errorOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      data-testid="review-card"
      aria-labelledby={`${ids}-reason`}
      className={`flex min-w-0 flex-col gap-4 rounded-2xl border bg-card p-4 text-card-foreground shadow-sm sm:p-6 ${
        decided || readOnly ? "border-l-4 border-l-border" : "border-l-4 border-l-emerald-500"
      }`}
    >
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1.5">
          <AlertTriangle
            aria-hidden="true"
            className={`h-3.5 w-3.5 ${decided || readOnly ? "text-muted-foreground" : "text-emerald-600"}`}
          />
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {decided ? "Decided" : readOnly ? "Read only" : "Needs your decision"}
          </span>
        </div>
        <h2 id={`${ids}-reason`} data-testid="reason-line" className="break-words text-lg font-semibold leading-snug">
          {item.reasonLine}
        </h2>
      </div>

      {item.image ? (
        <PageImageBand
          src={item.image.signedUrl}
          pageNo={item.image.pageNo}
          rowIndex={item.image.rowIndex}
          tableTop={item.image.tableTop}
          tableBottom={item.image.tableBottom}
          onError={onImageError}
        />
      ) : null}

      {item.readA || item.readB ? (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <ReadBlock label="Read A" cell={item.readA} testId="read-a" />
          <ReadBlock label="Read B" cell={item.readB} testId="read-b" />
        </div>
      ) : null}

      {item.details.length ? (
        <dl className="grid grid-cols-1 gap-x-3 gap-y-1 rounded-xl border border-border/70 bg-muted/20 p-3 text-sm sm:grid-cols-[auto_1fr]">
          {item.details.map((d) => (
            <div key={d.label} className="contents">
              <dt className="text-muted-foreground">{d.label}</dt>
              <dd className="break-words font-medium">{d.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {item.otherSources ? (
        <section
          data-testid="other-sources"
          aria-labelledby={`${ids}-other`}
          className="rounded-xl border border-dashed border-border/60 bg-muted/10 p-3 text-sm"
        >
          <h3 id={`${ids}-other`} className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {item.otherSources.label}
          </h3>
          <ul className="mt-1.5 flex flex-wrap gap-2">
            {item.otherSources.values.map((o) => (
              <li
                key={o.source}
                data-testid="other-source"
                className="break-words rounded-full bg-background px-2.5 py-1 text-xs font-medium text-foreground shadow-sm"
              >
                {o.source === "ERP" ? "ERP" : "Sheet"} {o.value ?? `not in ${sourceLabel(o.source)}`}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {error ? (
        <p
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-900/40 dark:bg-red-900/30 dark:text-red-300"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="break-words">{error.text}</span>
        </p>
      ) : null}

      {readOnly && readOnlyReason ? (
        <p className="rounded-xl bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{readOnlyReason}</p>
      ) : null}

      {showActions ? (
        <div className="flex flex-col gap-4 border-t border-border/60 pt-4">
          {canEnter ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-value`}>Value</Label>
              <Input
                id={`${ids}-value`}
                value={value}
                autoComplete="off"
                inputMode={item.valueField === "qty" ? "decimal" : undefined}
                maxLength={64}
                className="sm:max-w-xs"
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.preventDefault(); // Enter never decides
                }}
              />
            </div>
          ) : null}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-note`}>Note</Label>
            <Textarea
              id={`${ids}-note`}
              value={note}
              maxLength={500}
              onChange={(e) => {
                setNote(e.target.value);
                writeNote(item.id, e.target.value);
              }}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2.5">
            {ACTION_ORDER.filter((a) => item.actions.includes(a)).map((a) => {
              const ActionIcon = ACTION_ICON[a];
              return (
                <Button
                  key={a}
                  type="button"
                  size="lg"
                  variant={a === "CANNOT_DETERMINE" ? "outline" : "default"}
                  disabled={busy || (a === "ACCEPT_READ" && !item.acceptable)}
                  className={a !== "CANNOT_DETERMINE" ? "bg-emerald-600 text-white hover:bg-emerald-600/90" : ""}
                  onClick={() => void act(a)}
                >
                  <ActionIcon aria-hidden="true" className="h-4 w-4" />
                  {ACTION_LABEL[a]}
                </Button>
              );
            })}
          </div>
          {/* EP-15 re-reconciles the whole run before it answers, which takes several seconds. */}
          {busy ? (
            <p role="status" data-testid="review-saving" className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
              Saving your decision and re-checking all three sources…
            </p>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
