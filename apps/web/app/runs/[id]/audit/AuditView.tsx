"use client";

// Client half of SCR-08 `/runs/[id]/audit` (006-web T062; contracts/screens.md SCR-08). The
// server page (page.tsx) does the one read of EP-20 and EP-19; this component only adds the
// actor filter (SCR-08-AC3) over the already-fetched audit rows — no extra network call, since
// the mock API (and EP-20 itself, FR-034) always returns the run's full list for `?actor=`
// unset, and filtering the one small list client-side is simpler than a second round trip.

import { ArrowRight, History, ShieldCheck, User2 } from "lucide-react";
import { useMemo, useState } from "react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Empty } from "@/components/states";
import type { components } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/time";
import { auditActionLabel, stageText } from "@/lib/wording";

export type AuditEntry = components["schemas"]["AuditEntryOut"];
export type RunEvent = components["schemas"]["RunEventOut"];

export type AuditViewProps = {
  entries: AuditEntry[];
  events: RunEvent[];
};

/** A plain-words "key: value, key: value" rendering of an audit before/after object, never raw JSON. */
function formatValues(values: Record<string, unknown> | null): string {
  if (!values) return "—";
  const parts = Object.entries(values)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}: ${v === null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return parts.length > 0 ? parts.join(", ") : "—";
}

const EVENT_STATUS_LABEL: Record<RunEvent["status"], string> = {
  STARTED: "Started",
  OK: "OK",
  RETRY: "Retrying",
  FAILED: "Failed",
};

const EVENT_STATUS_DOT: Record<RunEvent["status"], string> = {
  STARTED: "bg-blue-500",
  OK: "bg-emerald-500",
  RETRY: "bg-amber-500",
  FAILED: "bg-red-500",
};

const EVENT_STATUS_TEXT: Record<RunEvent["status"], string> = {
  STARTED: "text-blue-700 dark:text-blue-400",
  OK: "text-emerald-700 dark:text-emerald-400",
  RETRY: "text-amber-700 dark:text-amber-400",
  FAILED: "text-red-700 dark:text-red-400",
};

export function AuditView({ entries, events }: AuditViewProps) {
  const [actor, setActor] = useState("");
  const actors = useMemo(
    () => Array.from(new Set(entries.map((e) => e.actor))).sort(),
    [entries],
  );
  const filtered = actor ? entries.filter((e) => e.actor === actor) : entries;

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-3 py-6 sm:px-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-[family-name:var(--font-fraunces)] text-2xl font-medium tracking-tight">Audit</h1>
        <p className="text-sm text-muted-foreground">
          Every decision and every run event, kept as a permanent record.
        </p>
      </div>

      <section aria-labelledby="audit-log-heading" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h2 id="audit-log-heading" className="inline-flex items-center gap-2 text-base font-semibold">
            <ShieldCheck className="size-4 text-muted-foreground" aria-hidden="true" />
            Audit log
          </h2>
          <label className="flex items-center gap-2 text-sm">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">
              <User2 className="size-3.5" aria-hidden="true" />
              Actor
            </span>
            <select
              data-testid="audit-actor-filter"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2.5 text-sm shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="">All actors</option>
              {actors.map((a) => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
          </label>
        </div>
        {filtered.length === 0 ? (
          <Empty text="No audit entries yet." />
        ) : (
          <div className="overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Time</TableHead>
                  <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Actor</TableHead>
                  <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Action</TableHead>
                  <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Entity</TableHead>
                  <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Channel</TableHead>
                  <TableHead className="bg-muted/40 text-xs font-semibold uppercase tracking-wide text-muted-foreground" colSpan={2}>
                    Before → After
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((entry) => (
                  <TableRow key={entry.id} data-testid={`audit-row-${entry.id}`} className="align-top">
                    <TableCell className="whitespace-nowrap text-muted-foreground">
                      <time dateTime={entry.at}>{formatDateTime(entry.at)}</time>
                    </TableCell>
                    <TableCell className="font-medium">{entry.actor}</TableCell>
                    <TableCell>{auditActionLabel(entry.action)}</TableCell>
                    <TableCell className="text-muted-foreground">{entry.entity}</TableCell>
                    <TableCell className="text-muted-foreground">{entry.channel}</TableCell>
                    <TableCell className="whitespace-normal [overflow-wrap:anywhere]">
                      <span
                        data-testid={`audit-before-${entry.id}`}
                        className="rounded-md border border-border/60 bg-muted/40 px-1.5 py-0.5 text-xs text-muted-foreground"
                      >
                        {formatValues(entry.before)}
                      </span>
                    </TableCell>
                    <TableCell className="whitespace-normal [overflow-wrap:anywhere]">
                      <span className="inline-flex items-center gap-1.5">
                        <ArrowRight className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
                        <span
                          data-testid={`audit-after-${entry.id}`}
                          className="rounded-md border border-emerald-300/60 bg-emerald-50 px-1.5 py-0.5 text-xs font-medium text-emerald-800 dark:border-emerald-700/50 dark:bg-emerald-950/30 dark:text-emerald-300"
                        >
                          {formatValues(entry.after)}
                        </span>
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>

      <section aria-labelledby="audit-events-heading" className="flex flex-col gap-3">
        <h2 id="audit-events-heading" className="inline-flex items-center gap-2 text-base font-semibold">
          <History className="size-4 text-muted-foreground" aria-hidden="true" />
          Run events
        </h2>
        {events.length === 0 ? (
          <Empty text="No audit entries yet." />
        ) : (
          <div className="rounded-xl border border-border/70 bg-card p-4 shadow-sm sm:p-5">
            <ol className="flex flex-col">
              {events.map((event, i) => (
                <li key={event.id} data-testid={`event-row-${event.id}`} className="relative flex gap-4 pb-6 last:pb-0">
                  <div className="relative flex w-2.5 shrink-0 flex-col items-center">
                    <span
                      className={cn(
                        "z-10 mt-1.5 size-2.5 shrink-0 rounded-full ring-4 ring-card",
                        EVENT_STATUS_DOT[event.status],
                      )}
                      aria-hidden="true"
                    />
                    {i < events.length - 1 && (
                      <span className="absolute top-3 h-full w-px bg-border" aria-hidden="true" />
                    )}
                  </div>
                  <div className="flex flex-1 flex-col gap-0.5 pt-0">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
                      <p className="text-sm font-medium">{stageText(event.stage)}</p>
                      <time dateTime={event.at} className="text-xs text-muted-foreground">
                        {formatDateTime(event.at)}
                      </time>
                    </div>
                    <div className="flex items-center gap-2 text-xs">
                      <span className={cn("font-medium", EVENT_STATUS_TEXT[event.status])}>
                        {EVENT_STATUS_LABEL[event.status]}
                      </span>
                      <span className="text-muted-foreground">·</span>
                      <span className="text-muted-foreground">
                        {event.duration_ms === null ? "—" : `${event.duration_ms} ms`}
                      </span>
                    </div>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        )}
      </section>
    </div>
  );
}
