"use client";

// Delete one run from the runs table (POST /runs/{id}/delete with {"confirm": true}; CONTROLLER
// only, the API stays the authority). The row itself navigates on click, so every event here
// stops at the button. The audit log keeps the run's history plus a "Run deleted" entry.

import { Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useMe } from "@/app/providers";
import { errorStateText } from "@/components/states";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { api, ApiError } from "@/lib/api";

export function DeleteRunButton({ runId, monthLabel }: { runId: string; monthLabel: string }) {
  const me = useMe();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!me.can.createRun) return null;

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.post("/runs/{id}/delete", { path: { id: runId }, body: { confirm: true } });
      setOpen(false);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? errorStateText(err) : "Could not delete this run.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
      className="inline-flex"
    >
      <button
        type="button"
        aria-label={`Delete run ${monthLabel}`}
        title="Delete run"
        onClick={() => setOpen(true)}
        className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-danger-soft hover:text-danger-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      >
        <Trash2 className="h-4 w-4" aria-hidden="true" />
      </button>
      <AlertDialog open={open} onOpenChange={(next) => !busy && setOpen(next)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this run?</AlertDialogTitle>
            <AlertDialogDescription>
              {monthLabel}: its files, results, review decisions and report are removed for good. The audit
              log keeps its history and records the delete.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error ? (
            <p role="alert" className="text-sm text-danger-ink">
              {error}
            </p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              className="bg-danger text-white hover:bg-danger/90"
              onClick={(e) => {
                e.preventDefault();
                void remove();
              }}
            >
              {busy ? "Deleting…" : "Delete run"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </span>
  );
}
