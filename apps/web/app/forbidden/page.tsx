"use client";

// 006-web T028 (US1, SCR-01-AC3): shown when EP-02 answers 403 NOT_ALLOWED (lib/me.ts /
// app/providers.tsx MeProvider redirects here). This page never calls EP-02 itself
// (lib/me.ts isSignedOutPath does not include it, but providers.tsx `asksWhoAmI` excludes it
// explicitly — no run data is requested from here).

import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { getBrowserSupabase } from "@/lib/supabase";

export default function ForbiddenPage() {
  const router = useRouter();

  async function signOut() {
    await getBrowserSupabase().auth.signOut({ scope: "local" });
    router.push("/login");
  }

  return (
    <div className="mx-auto flex w-full max-w-sm flex-1 flex-col items-start justify-center gap-4 p-4">
      <p>Your email is not on the list for this app</p>
      <Button type="button" onClick={signOut}>
        Sign out
      </Button>
    </div>
  );
}
