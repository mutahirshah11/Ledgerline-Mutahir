"use client";

// Client half of SCR-08 `/runs/[id]/audit` (006-web T062; contracts/screens.md SCR-08). The
// server page (page.tsx) does the one read of EP-20 and EP-19; this component only adds the
// actor filter (SCR-08-AC3) over the already-fetched audit rows — no extra network call, since
// the mock API (and EP-20 itself, FR-034) always returns the run's full list for `?actor=`
// unset, and filtering the one small list client-side is simpler than a second round trip.

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

export function AuditView({ entries, events }: AuditViewProps) {
  const [actor, setActor] = useState("");
  const actors = useMemo(
    () => Array.from(new Set(entries.map((e) => e.actor))).sort(),
    [entries],
  );
  const filtered = actor ? entries.filter((e) => e.actor === actor) : entries;

  return (
    <div className="flex flex-col gap-8 p-4">
      <h1 className="text-lg font-semibold">Audit</h1>

      <section aria-labelledby="audit-log-heading" className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-4">
          <h2 id="audit-log-heading" className="text-base font-semibold">Audit log</h2>
          <label className="flex items-center gap-2 text-sm">
            Actor
            <select
              data-testid="audit-actor-filter"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              className="rounded-md border border-input bg-transparent px-2 py-1 text-sm"
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
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Actor</TableHead>
                <TableHead>Action</TableHead>
                <TableHead>Entity</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead>Before</TableHead>
                <TableHead>After</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((entry) => (
                <TableRow key={entry.id} data-testid={`audit-row-${entry.id}`}>
                  <TableCell>
                    <time dateTime={entry.at}>{formatDateTime(entry.at)}</time>
                  </TableCell>
                  <TableCell>{entry.actor}</TableCell>
                  <TableCell>{auditActionLabel(entry.action)}</TableCell>
                  <TableCell>{entry.entity}</TableCell>
                  <TableCell>{entry.channel}</TableCell>
                  <TableCell data-testid={`audit-before-${entry.id}`}>{formatValues(entry.before)}</TableCell>
                  <TableCell data-testid={`audit-after-${entry.id}`}>{formatValues(entry.after)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <section aria-labelledby="audit-events-heading" className="flex flex-col gap-3">
        <h2 id="audit-events-heading" className="text-base font-semibold">Run events</h2>
        {events.length === 0 ? (
          <Empty text="No audit entries yet." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Stage</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Time</TableHead>
                <TableHead>Duration</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((event) => (
                <TableRow key={event.id} data-testid={`event-row-${event.id}`}>
                  <TableCell>{stageText(event.stage)}</TableCell>
                  <TableCell>{EVENT_STATUS_LABEL[event.status]}</TableCell>
                  <TableCell>
                    <time dateTime={event.at}>{formatDateTime(event.at)}</time>
                  </TableCell>
                  <TableCell>{event.duration_ms === null ? "—" : `${event.duration_ms} ms`}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>
    </div>
  );
}
