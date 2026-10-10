import { BookOpen, ClipboardList, FileSpreadsheet, Plus, Rocket } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export type RunsEmptyStateProps = {
  canCreateRun: boolean;
};

const SOURCES = [
  { icon: BookOpen, title: "Receiving register", body: "Scan or photograph the handwritten pages." },
  { icon: ClipboardList, title: "ERP goods-received notes", body: "Connect your ERP once. Notes import each month." },
  { icon: FileSpreadsheet, title: "Storekeeper's sheet", body: "Upload the spreadsheet or a photo of it." },
] as const;

export function RunsEmptyState({ canCreateRun }: RunsEmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-4 rounded-3xl border border-dashed border-border bg-card px-6 py-16 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary-soft text-primary">
        <Rocket className="h-7 w-7" aria-hidden="true" />
      </div>
      {/* "No runs yet." is the SCR-02-AC4 contract string (e2e/scr-02-runs.spec.ts); keep it
       * verbatim even inside this richer empty state. */}
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">No runs yet.</p>
      <h2 className="font-heading text-2xl font-bold tracking-tight text-foreground">Let&apos;s run your first reconciliation</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Add these three records and Ledgerline will compare them line by line.
      </p>
      <div className="mt-2 grid w-full max-w-3xl gap-3 sm:grid-cols-3">
        {SOURCES.map(({ icon: Icon, title, body }, i) => (
          <div key={title} className="rounded-2xl border border-border bg-muted/30 p-4 text-left">
            <div className="flex items-center justify-between">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary-soft text-primary">
                <Icon className="h-4.5 w-4.5" aria-hidden="true" />
              </span>
              <span className="text-xs font-semibold text-muted-foreground">0{i + 1}</span>
            </div>
            <p className="mt-3 text-sm font-semibold text-foreground">{title}</p>
            <p className="mt-1 text-xs text-muted-foreground">{body}</p>
          </div>
        ))}
      </div>
      {canCreateRun ? (
        <Button render={<Link href="/runs/new" />} className="mt-4 gap-2 bg-primary hover:bg-primary-hover">
          <Plus className="h-4 w-4" aria-hidden="true" />
          New run
        </Button>
      ) : null}
    </div>
  );
}
