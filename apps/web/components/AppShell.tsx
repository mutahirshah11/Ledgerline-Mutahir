"use client";

// Branded sidebar shell for every signed-in screen (SCR-02 .. SCR-08). Hidden on sign-out pages
// (/login, /forbidden, /__test__/*) so those keep their own plain layout.
//
// Two permanent top-level items so the product's two jobs are never just a small in-page button:
// "Dashboard" (/runs, the reconciliation-runs overview) and "Start a run" (/runs/new, the actual
// upload-and-reconcile flow) — named "Start a run" rather than "New run" so it never collides
// with RunsTable.tsx's own "New run" button under Playwright's accessible-name matching (e.g.
// scr-01-login.spec.ts, scr-02-runs.spec.ts, smoke.spec.ts all query getByRole("link", {name:
// "New run"})). Gated by `can.createRun`, same as that button, since a REVIEWER can't use it.
//
// Dark teal base (Ledgerline 2026 redesign tokens, apps/web/app/globals.css :root) with primary
// teal used only as a sparing accent (active state, avatar) — a single saturated colour
// everywhere reads as dated, not premium.

import { FilePlus2, FileSearch, LayoutGrid, ListChecks, LogOut, ScrollText, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { useMe } from "@/app/providers";
import { isSignedOutPath } from "@/lib/me";
import { getBrowserSupabase } from "@/lib/supabase";

function initials(label: string): string {
  const parts = label.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0]![0]}${parts[1]![0]}`.toUpperCase();
  return (label.slice(0, 2) || "?").toUpperCase();
}

/** `/runs/{id}/...` (not `/runs`, `/runs/new` or `/runs/logs`) -> that run's id, for the
 * contextual sub-nav. */
function runIdFromPath(pathname: string): string | null {
  const m = pathname.match(/^\/runs\/([^/]+)/);
  if (!m || m[1] === "new" || m[1] === "logs") return null;
  return m[1]!;
}

const RUN_TABS = [
  { href: "", label: "Overview", icon: LayoutGrid },
  { href: "/results", label: "Results", icon: ListChecks },
  { href: "/review", label: "Review", icon: FileSearch },
  { href: "/proof", label: "Proof", icon: ShieldCheck },
  { href: "/audit", label: "Audit", icon: ScrollText },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const { status, person, can } = useMe();

  // `children` (the page's <main>) must stay in the SAME tree position whether or not the
  // sidebar shows, or React treats it as a different subtree and remounts it — discarding all
  // local state (e.g. an in-progress file upload) the instant `status` finishes "loading" and
  // flips `showShell` true. The fix: always render the same wrapper; only the <aside> itself is
  // conditional, as a sibling that doesn't affect `children`'s position.
  //
  // The shell's visibility depends only on the PATH, not on `status`/`person` being loaded yet:
  // a protected route only ever renders here once the server component has already confirmed the
  // visitor is signed in (serverApi redirects to /login otherwise), so the brief client-side
  // "loading" window (while EP-02 /me answers) is never actually a signed-out state — gating on
  // `status === "ready"` just made the whole sidebar flash away and back on every load/refresh.
  // Person-dependent bits (name/role, "Start a run"'s `can.createRun`) render as skeletons
  // instead until `status` settles.
  const showShell = pathname !== "/forbidden" && !isSignedOutPath(pathname);
  const personReady = status === "ready" && person !== null;

  async function signOut() {
    await getBrowserSupabase().auth.signOut({ scope: "local" });
    router.push("/login");
  }

  const runsActive = pathname === "/runs";
  const newRunActive = pathname.startsWith("/runs/new");
  const logsActive = pathname.startsWith("/runs/logs");
  const currentRunId = runIdFromPath(pathname);

  return (
    <div className="flex min-h-screen w-full flex-1">
      {showShell ? (
        <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col justify-between overflow-y-auto border-r border-sidebar-border bg-sidebar text-sidebar-foreground/70 lg:flex">
          <div>
            <div className="flex items-center gap-2.5 px-5 py-6">
              <div className="flex h-7 w-7 shrink-0 flex-col items-center justify-center gap-[3px] rounded-[8px] bg-primary px-[7px]">
                <span className="h-[2.5px] w-full rounded-full bg-white" />
                <span className="h-[2.5px] w-full rounded-full bg-white" />
                <span className="h-[2.5px] w-[60%] self-start rounded-full bg-success" />
              </div>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold leading-tight text-white">Ledgerline</p>
                <p className="truncate text-[10px] font-medium tracking-wide text-sidebar-foreground/50 uppercase">
                  Reconciliation
                </p>
              </div>
            </div>
            <nav aria-label="Primary" className="flex flex-col gap-0.5 px-3">
              <Link
                href="/runs"
                className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                  runsActive
                    ? "bg-sidebar-accent text-white"
                    : "text-sidebar-foreground/60 hover:bg-sidebar-accent/60 hover:text-white"
                }`}
              >
                <LayoutGrid className="h-4 w-4" aria-hidden="true" />
                Dashboard
              </Link>
              {!personReady ? (
                <div className="mx-3 my-1.5 h-[34px] animate-pulse rounded-md bg-sidebar-accent/40" />
              ) : can.createRun ? (
                <Link
                  href="/runs/new"
                  className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                    newRunActive
                      ? "bg-sidebar-accent text-white"
                      : "text-sidebar-foreground/60 hover:bg-sidebar-accent/60 hover:text-white"
                  }`}
                >
                  <FilePlus2 className="h-4 w-4" aria-hidden="true" />
                  Start a run
                </Link>
              ) : null}
            </nav>

            {currentRunId ? (
              <div className="mt-5 px-3">
                <p className="px-3 text-[10px] font-semibold tracking-wide text-sidebar-foreground/50 uppercase">This run</p>
                <nav aria-label="This run" className="mt-1.5 flex flex-col gap-0.5">
                  {RUN_TABS.map(({ href, label, icon: Icon }) => {
                    const target = `/runs/${currentRunId}${href}`;
                    const active = pathname === target;
                    return (
                      <Link
                        key={label}
                        href={target}
                        className={`flex items-center gap-2.5 rounded-md border-l-2 px-3 py-1.5 text-sm transition-colors ${
                          active
                            ? "border-primary bg-sidebar-accent font-medium text-white"
                            : "border-transparent text-sidebar-foreground/60 hover:bg-sidebar-accent/60 hover:text-white"
                        }`}
                      >
                        <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                        {label}
                      </Link>
                    );
                  })}
                </nav>
              </div>
            ) : null}
          </div>
          <div className="border-t border-sidebar-border p-3">
            <div className="flex items-center gap-2.5 rounded-md px-2 py-2">
              {personReady && person ? (
                <>
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-semibold text-white">
                    {initials(person.displayName || person.email)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-white">{person.displayName}</p>
                    <p className="truncate text-[11px] text-sidebar-foreground/50">{person.role}</p>
                  </div>
                </>
              ) : (
                <>
                  <div className="h-8 w-8 shrink-0 animate-pulse rounded-full bg-sidebar-accent/40" />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <div className="h-3 w-24 animate-pulse rounded bg-sidebar-accent/40" />
                    <div className="h-2.5 w-14 animate-pulse rounded bg-sidebar-accent/40" />
                  </div>
                </>
              )}
            </div>
            <button
              type="button"
              onClick={() => void signOut()}
              className="mt-1 flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm text-sidebar-foreground/60 transition-colors hover:bg-sidebar-accent/60 hover:text-white"
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
              Sign out
            </button>
            {/* Every run ever attempted, including failed/old ones the dashboard itself now
             * hides to stay uncluttered — nothing is ever deleted (apps/api audit_log is
             * append-only), so this is where that full history lives instead. */}
            <Link
              href="/runs/logs"
              className={`mt-1 flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm transition-colors ${
                logsActive
                  ? "bg-sidebar-accent text-white"
                  : "text-sidebar-foreground/40 hover:bg-sidebar-accent/60 hover:text-white"
              }`}
            >
              <ScrollText className="h-4 w-4" aria-hidden="true" />
              Logs
            </Link>
          </div>
        </aside>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
