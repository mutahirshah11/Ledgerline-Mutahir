"use client";

// 006-web T026 (US1, SCR-01-AC1/AC2, FR-001/FR-002): sign in with a magic link (default) or, for
// the two seeded demo accounts, email + password. No sign-up is ever offered (shouldCreateUser
// false). /login must never auto-forward a signed-in visitor (research R3) — it is a plain form,
// nothing here checks the current session; a signed-in person who lands here just sees the form
// again, which is harmless (and is what keeps a dead/expired session from looping).
//
// `expired=1` (added by lib/api.ts / lib/server-api.ts on a 401) signs out the local session
// before the person tries again, since a server component cannot clear cookies itself.

import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Lock, Mail, ShieldCheck } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ME_QUERY_KEY } from "@/app/providers";
import { ApiError, api } from "@/lib/api";
import { safeNext } from "@/lib/me";
import { getBrowserSupabase } from "@/lib/supabase";

type OtpState = "form" | "sending" | "sent" | "error";
type PasswordState = "form" | "signing-in" | "error";

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

  const [usePassword, setUsePassword] = useState(false);

  const [otpEmail, setOtpEmail] = useState("");
  const [otpState, setOtpState] = useState<OtpState>("form");

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

  async function sendLink(e: React.FormEvent) {
    e.preventDefault();
    setOtpState("sending");
    const { error } = await getBrowserSupabase().auth.signInWithOtp({
      email: otpEmail,
      options: {
        shouldCreateUser: false,
        emailRedirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`,
      },
    });
    setOtpState(error ? "error" : "sent");
  }

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
    <div className="flex min-h-screen w-full flex-1 flex-col lg:flex-row">
      <aside
        className="relative hidden flex-col justify-between overflow-hidden bg-[#0a2e24] p-10 text-emerald-50 lg:flex lg:w-1/2"
        style={{
          backgroundImage:
            "radial-gradient(rgba(52, 211, 153, 0.16) 1px, transparent 1px), linear-gradient(180deg, #0a2e24 0%, #0b3328 60%, #0d3a2e 100%)",
          backgroundSize: "22px 22px, 100% 100%",
        }}
      >
        <div className="relative">
          <div className="mb-20 flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-emerald-400 text-sm font-bold text-emerald-950">
              L
            </div>
            <span className="text-sm font-semibold tracking-wide text-white">Ledgerline</span>
          </div>
          <h2 className="max-w-md text-5xl leading-[1.05] font-semibold text-balance text-white">
            Three sources, <span className="underline decoration-emerald-400 decoration-4 underline-offset-4">one proven</span>{" "}
            <span className="font-serif text-emerald-300 italic">reconciliation.</span>
          </h2>
          <p className="mt-5 max-w-sm text-sm text-emerald-100/75">
            Register, ERP and store sheet, read and reconciled automatically — every match, mismatch
            and gap accounted for, nothing guessed.
          </p>
          <div className="mt-14 flex flex-col gap-6">
            {[
              ["01", "Every row proven", "Counts and totals tie back to the inputs, every run."],
              ["02", "Every figure traceable", "Down to the register page or ERP row that produced it."],
              ["03", "Review only what's doubtful", "A person resolves the exceptions, not every receipt."],
            ].map(([n, title, body]) => (
              <div key={n} className="border-t border-emerald-800/50 pt-4">
                <p className="text-xs font-medium tracking-wide text-emerald-400">{n}</p>
                <p className="mt-1 text-sm font-medium text-white">{title}</p>
                <p className="mt-0.5 text-sm text-emerald-100/65">{body}</p>
              </div>
            ))}
          </div>
        </div>
        <div className="relative flex items-center gap-4 text-[11px] tracking-wide text-emerald-300/55 uppercase">
          <span className="flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" /> Audited
          </span>
          <span>·</span>
          <span>Kestrel Industries</span>
          <span>·</span>
          <span>Internal system</span>
        </div>
      </aside>

      <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 p-6 lg:w-1/2 lg:max-w-md lg:p-16">
        <div className="lg:hidden">
          <div className="mb-6 flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-emerald-600 text-sm font-bold text-white">
              L
            </div>
            <span className="text-sm font-semibold">Ledgerline</span>
          </div>
        </div>
        <div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-600/30 bg-emerald-50 px-2.5 py-1 text-[11px] font-medium tracking-wide text-emerald-700 uppercase dark:border-emerald-400/20 dark:bg-emerald-950/40 dark:text-emerald-300">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            Controller &amp; reviewer access
          </span>
          <h1 className="mt-4 text-3xl font-semibold tracking-tight">Welcome back</h1>
          <p className="mt-1 text-sm text-muted-foreground">Sign in to reconcile this month&apos;s receipts.</p>
        </div>

      {!usePassword && (
        <form className="flex flex-col gap-4" onSubmit={sendLink}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="otp-email">Email</Label>
            <div className="relative">
              <Mail className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input
                id="otp-email"
                type="email"
                autoComplete="email"
                required
                className="pl-9"
                value={otpEmail}
                onChange={(e) => setOtpEmail(e.target.value)}
              />
            </div>
          </div>
          <Button type="submit" disabled={otpState === "sending"} className="group gap-2 rounded-lg bg-[#0b3328] py-5 hover:bg-[#0b3328]/90">
            Send link
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
          </Button>
          {otpState === "sent" && (
            <p role="status">Check your email for a sign-in link</p>
          )}
          {otpState === "error" && (
            <p role="alert">That email could not be sent a link</p>
          )}
        </form>
      )}

      {usePassword && (
        <form className="flex flex-col gap-4" onSubmit={signInWithPassword}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-email">Email</Label>
            <div className="relative">
              <Mail className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input
                id="pw-email"
                type="email"
                autoComplete="email"
                required
                className="pl-9"
                value={pwEmail}
                onChange={(e) => setPwEmail(e.target.value)}
              />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-password">Password</Label>
            <div className="relative">
              <Lock className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input
                id="pw-password"
                type="password"
                autoComplete="current-password"
                required
                className="pl-9"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </div>
          <Button type="submit" disabled={pwState === "signing-in"} className="group gap-2 rounded-lg bg-[#0b3328] py-5 hover:bg-[#0b3328]/90">
            Sign in
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
          </Button>
          {pwState === "error" && <p role="alert">That email and password did not work</p>}
        </form>
      )}

      <Button
        type="button"
        variant="ghost"
        onClick={() => setUsePassword((v) => !v)}
      >
        {usePassword ? "Use a sign-in link instead" : "Use password (demo account)"}
      </Button>
      </div>
    </div>
  );
}
