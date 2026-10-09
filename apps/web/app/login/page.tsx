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
    <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 p-4">
      <h1 className="text-lg font-semibold">Sign in</h1>

      {!usePassword && (
        <form className="flex flex-col gap-3" onSubmit={sendLink}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="otp-email">Email</Label>
            <Input
              id="otp-email"
              type="email"
              autoComplete="email"
              required
              value={otpEmail}
              onChange={(e) => setOtpEmail(e.target.value)}
            />
          </div>
          <Button type="submit" disabled={otpState === "sending"}>
            Send link
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
        <form className="flex flex-col gap-3" onSubmit={signInWithPassword}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-email">Email</Label>
            <Input
              id="pw-email"
              type="email"
              autoComplete="email"
              required
              value={pwEmail}
              onChange={(e) => setPwEmail(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-password">Password</Label>
            <Input
              id="pw-password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <Button type="submit" disabled={pwState === "signing-in"}>
            Sign in
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
  );
}
