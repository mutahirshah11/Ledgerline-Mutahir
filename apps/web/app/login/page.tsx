"use client";

// 006-web T026 revision (US1, FR-002): email + password only. No sign-up is ever offered. /login
// must never auto-forward a signed-in visitor (research R3) — it is a plain form, nothing here
// checks the current session; a signed-in person who lands here just sees the form again, which
// is harmless (and is what keeps a dead/expired session from looping).
//
// `expired=1` (added by lib/api.ts / lib/server-api.ts on a 401) signs out the local session
// before the person tries again, since a server component cannot clear cookies itself.
//
// Visual redesign (2026 Teal+Lime handoff, design_handoff_ledgerline_redesign): 2-column split,
// form left / brand panel right. Labels stay "Email"/"Password" and the button stays "Sign in"
// (not the mockup's "Work email") — e2e/scr-01-login.spec.ts pins those exact accessible names.
// No SSO button or "Forgot it?" link: neither SSO nor password reset exists in this app.

import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, Lock, Mail, Sparkles } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ME_QUERY_KEY } from "@/app/providers";
import { ApiError, api } from "@/lib/api";
import { safeNext } from "@/lib/me";
import { getBrowserSupabase } from "@/lib/supabase";

type PasswordState = "form" | "signing-in" | "error";

const TRUST_BULLETS = [
  "Three sources compared line by line",
  "Mismatches shown side by side",
  "Every decision recorded for audit",
];

// A 10x5 sample grid for the brand panel — illustrative only, not real run data.
const SAMPLE_GRID = Array.from({ length: 50 }, (_, i) => (i === 12 || i === 27 ? "warning" : "success"));

function Logo({ dark = false }: { dark?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div className="flex h-7 w-7 shrink-0 flex-col items-center justify-center gap-[3px] rounded-lg bg-primary px-[7px]">
        <span className="h-[2.5px] w-full rounded-full bg-white" />
        <span className="h-[2.5px] w-full rounded-full bg-white" />
        <span className="h-[2.5px] w-3/5 self-start rounded-full bg-success" />
      </div>
      <span className={`font-heading text-sm font-bold ${dark ? "text-white" : "text-ink"}`}>Ledgerline</span>
    </div>
  );
}

// Next 16 build requires useSearchParams() to sit under a Suspense boundary
// (node_modules/next/dist/docs error "missing-suspense-with-csr-bailout").
export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const next = safeNext(searchParams.get("next"));

  const [pwEmail, setPwEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pwState, setPwState] = useState<PasswordState>("form");

  // signOut({scope:"local"}) only once per `expired=1` visit.
  const handledExpired = useRef(false);
  useEffect(() => {
    if (searchParams.get("expired") !== "1" || handledExpired.current) return;
    handledExpired.current = true;
    void getBrowserSupabase().auth.signOut({ scope: "local" });
  }, [searchParams]);

  async function signInWithPassword(e: React.FormEvent) {
    e.preventDefault();
    setPwState("signing-in");
    const { error } = await getBrowserSupabase().auth.signInWithPassword({
      email: pwEmail,
      password,
    });
    if (error) {
      setPwState("error");
      return;
    }
    // A fresh sign-in means any previously cached "who am I" answer (a different person, or none)
    // is stale (FR-003). Ask EP-02 here, before navigating anywhere: a protected page's own first
    // read (e.g. EP-04 on /runs) is not allowlist-gated the way EP-02 is, so landing on `next`
    // first and letting MeProvider catch a 403 afterwards would still let that first read through
    // (SCR-01-AC3 "no run data is requested" for someone off the allowlist).
    queryClient.removeQueries({ queryKey: ME_QUERY_KEY });
    try {
      const me = await api.get("/me");
      queryClient.setQueryData(ME_QUERY_KEY, me);
      router.push(next);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        router.push("/forbidden");
        return;
      }
      // Any other error: let the destination page's own error state handle it.
      router.push(next);
    }
  }

  return (
    <div className="flex min-h-screen w-full flex-1 flex-col bg-background lg:flex-row">
      <div className="flex w-full flex-1 flex-col justify-center gap-8 px-6 py-10 sm:px-10 lg:w-1/2 lg:px-16">
        <div className="flex items-center justify-between">
          <Logo />
          <Link
            href="/"
            className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            ← Back to site
          </Link>
        </div>

        <div className="mx-auto flex w-full max-w-[380px] flex-1 flex-col justify-center gap-6">
          <div>
            <div className="mb-5 flex h-11 w-11 items-center justify-center rounded-xl bg-primary-soft text-primary">
              <Sparkles className="h-5 w-5" aria-hidden="true" />
            </div>
            <h1 className="font-heading text-3xl font-bold tracking-tight text-ink">Welcome back</h1>
            <p className="mt-1.5 text-sm text-muted-foreground">
              Sign in to see how this month&apos;s reconciliation went.
            </p>
          </div>

          <form className="flex flex-col gap-4" onSubmit={signInWithPassword}>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="pw-email">Email</Label>
              <div className="relative">
                <Mail
                  className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="pw-email"
                  type="email"
                  autoComplete="email"
                  required
                  className="h-[46px] rounded-xl border-border-strong bg-muted pl-9 focus-visible:border-primary focus-visible:bg-white focus-visible:ring-4 focus-visible:ring-primary-soft"
                  value={pwEmail}
                  onChange={(e) => setPwEmail(e.target.value)}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="pw-password">Password</Label>
              <div className="relative">
                <Lock
                  className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="pw-password"
                  type="password"
                  autoComplete="current-password"
                  required
                  className="h-[46px] rounded-xl border-border-strong bg-muted pl-9 focus-visible:border-primary focus-visible:bg-white focus-visible:ring-4 focus-visible:ring-primary-soft"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            </div>
            <Button
              type="submit"
              disabled={pwState === "signing-in"}
              className="group h-12 gap-2 rounded-xl bg-primary text-base font-semibold text-primary-foreground shadow-[0_8px_20px_-8px_rgba(14,124,134,0.6)] hover:bg-primary-hover"
            >
              {pwState === "signing-in" ? "Signing you in…" : "Sign in"}
              <ArrowRight
                className="h-4 w-4 transition-transform group-hover:translate-x-0.5"
                aria-hidden="true"
              />
            </Button>
            {pwState === "error" && (
              <p role="alert" className="text-sm text-danger-ink">
                That email and password did not work
              </p>
            )}
          </form>

          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Lock className="h-3.5 w-3.5" aria-hidden="true" />
            Encrypted connection. Access managed by your admin.
          </p>
        </div>
      </div>

      <aside className="relative hidden w-1/2 shrink-0 items-center p-3 lg:flex">
        <div className="flex h-full w-full flex-col justify-center gap-10 rounded-[26px] bg-primary px-14 py-14 text-white">
          <h2 className="font-heading max-w-md text-4xl leading-[1.08] font-bold tracking-tight">
            Month-end, minus the detective work.
          </h2>

          <div className="rounded-2xl bg-white/10 p-6">
            <div className="grid grid-cols-10 gap-[3px]">
              {SAMPLE_GRID.map((state, i) => (
                <span
                  key={i}
                  className={`aspect-square rounded-[2.5px] ${
                    state === "warning" ? "bg-warning" : "bg-success"
                  }`}
                />
              ))}
            </div>
            <p className="mt-4 text-sm text-primary-tint-text">
              Each square is one delivery. Green ones matched on their own. You only open the yellow
              ones.
            </p>
          </div>

          <ul className="flex flex-col gap-4">
            {TRUST_BULLETS.map((text) => (
              <li key={text} className="flex items-center gap-2.5 text-sm font-medium text-white">
                <CheckCircle2 className="h-4.5 w-4.5 shrink-0 text-success" aria-hidden="true" />
                {text}
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  );
}
