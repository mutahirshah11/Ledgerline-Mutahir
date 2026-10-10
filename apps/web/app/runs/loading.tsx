import { Loader2 } from "lucide-react";

// Route-level Suspense fallback (Next.js convention) for /runs' server-fetched data — the 2026
// redesign's "Getting your runs ready" loading state, with a real spinner and skeleton bars
// instead of the shared bare-text <Loading/> (components/states.tsx), which stays untouched for
// every other screen.
export default function RunsLoading() {
  return (
    <div className="mx-auto w-full max-w-6xl p-6">
      <div role="status" className="rounded-3xl bg-primary-soft p-8">
        <div className="flex items-center gap-4">
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-white text-primary">
            <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
          </span>
          <div>
            <p className="font-heading text-lg font-bold text-foreground">Getting your runs ready</p>
            <p className="text-sm text-muted-foreground">Pulling your latest reconciliation results…</p>
          </div>
        </div>
        <div className="mt-6 flex flex-col gap-2">
          <div className="h-3 w-2/3 animate-pulse rounded-full bg-primary-soft-2" />
          <div className="h-3 w-5/12 animate-pulse rounded-full bg-primary-soft-2" />
        </div>
      </div>
    </div>
  );
}
