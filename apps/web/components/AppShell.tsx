"use client";

// Branded sidebar shell for every signed-in screen (SCR-02 .. SCR-08). Hidden on sign-out pages
// (/login, /forbidden, /__test__/*) so those keep their own plain layout. Deliberately does NOT
// duplicate "New run" (components/RunsTable.tsx already renders it on /runs for `can.createRun`)
// — a second link with that accessible name would collide under Playwright's substring role-name
// matching (e.g. scr-01-login.spec.ts, scr-02-runs.spec.ts).
//
// Dark teal base (Ledgerline 2026 redesign tokens, apps/web/app/globals.css :root) with primary
// teal used only as a sparing accent (active state, avatar) — a single saturated colour
// everywhere reads as dated, not premium.

import { FileSearch, LayoutGrid, ListChecks, LogOut, ScrollText, ShieldCheck } from "lucide-react";
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

/** `/runs/{id}/...` (not `/runs` or `/runs/new`) -> that run's id, for the contextual sub-nav. */
function runIdFromPath(pathname: string): string | null {
  const m = pathname.match(/^\/runs\/([^/]+)/);
  if (!m || m[1] === "new") return null;
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
  const { status, person } = useMe();

  // `children` (the page's <main>) must stay in the SAME tree position whether or not the
  // sidebar shows, or React treats it as a different subtree and remounts it — discarding all
  // local state (e.g. an in-progress file upload) the instant `status` finishes "loading" and
  // flips `showShell` true. The fix: always render the same wrapper; only the <aside> itself is
  // conditional, as a sibling that doesn't affect `children`'s position.
  const showShell = status === "ready" && person !== null && pathname !== "/forbidden" && !isSignedOutPath(pathname);

  async function signOut() {
    await getBrowserSupabase().auth.signOut({ scope: "local" });
    router.push("/login");
  }

  const runsActive = pathname === "/runs";
  const currentRunId = runIdFromPath(pathname);

  return (
    <div className="flex min-h-screen w-full flex-1">
      {showShell && person ? (
        <aside className="hidden w-64 shrink-0 flex-col justify-between border-r border-sidebar-border bg-sidebar text-sidebar-foreground/70 lg:flex">
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
                Runs
              </Link>
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
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-semibold text-white">
                {initials(person.displayName || person.email)}
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-white">{person.displayName}</p>
                <p className="truncate text-[11px] text-sidebar-foreground/50">{person.role}</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => void signOut()}
              className="mt-1 flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm text-sidebar-foreground/60 transition-colors hover:bg-sidebar-accent/60 hover:text-white"
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
              Sign out
            </button>
          </div>
        </aside>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
