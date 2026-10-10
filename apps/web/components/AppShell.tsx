"use client";

// Branded sidebar shell for every signed-in screen (SCR-02 .. SCR-08). Hidden on sign-out pages
// (/login, /auth/*, /forbidden, /__test__/*) so those keep their own plain layout. Deliberately
// does NOT duplicate "New run" (components/RunsTable.tsx already renders it on /runs for
// `can.createRun`) — a second link with that accessible name would collide under Playwright's
// substring role-name matching (e.g. scr-01-login.spec.ts, scr-02-runs.spec.ts).

import { LayoutGrid, LogOut } from "lucide-react";
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

  return (
    <div className="flex min-h-screen w-full flex-1">
      {showShell && person ? (
        <aside className="hidden w-60 shrink-0 flex-col justify-between border-r border-emerald-900/40 bg-[#0b3328] text-emerald-50 lg:flex">
          <div>
            <div className="flex items-center gap-2.5 px-5 py-6">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-emerald-400 text-sm font-bold text-emerald-950">
                L
              </div>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold leading-tight text-white">Ledgerline</p>
                <p className="truncate text-[10px] font-medium tracking-wide text-emerald-300/70 uppercase">
                  Reconciliation
                </p>
              </div>
            </div>
            <nav aria-label="Primary" className="flex flex-col gap-1 px-3">
              <Link
                href="/runs"
                className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                  runsActive
                    ? "bg-emerald-800/70 text-white"
                    : "text-emerald-100/75 hover:bg-emerald-800/40 hover:text-white"
                }`}
              >
                <LayoutGrid className="h-4 w-4" aria-hidden="true" />
                Runs
              </Link>
            </nav>
          </div>
          <div className="border-t border-emerald-900/40 p-3">
            <div className="flex items-center gap-2.5 rounded-md px-2 py-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-700 text-xs font-semibold text-white">
                {initials(person.displayName || person.email)}
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-white">{person.displayName}</p>
                <p className="truncate text-[11px] text-emerald-300/70">{person.role}</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => void signOut()}
              className="mt-1 flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm text-emerald-100/75 transition-colors hover:bg-emerald-800/40 hover:text-white"
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
