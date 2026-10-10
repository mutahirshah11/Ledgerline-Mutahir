import { ArrowRight, ShieldCheck } from "lucide-react";
import { cookies } from "next/headers";
import Link from "next/link";
import { redirect } from "next/navigation";

import { Button } from "@/components/ui/button";
import { createServerSupabase } from "@/lib/supabase";

// Public marketing landing (T020 revision): a signed-in visitor is sent straight to /runs, same
// as before; a signed-out visitor sees this page instead of bouncing straight to /login. "/" is
// in lib/me.ts SIGNED_OUT_PREFIXES so proxy.ts lets it through unauthenticated.

const VALUE_PROPS: ReadonlyArray<[string, string, string]> = [
  ["01", "Every row proven", "Counts and totals tie back to the register, ERP and store sheet, every run."],
  ["02", "Every figure traceable", "Down to the register page or ERP row that produced it — never composed."],
  ["03", "Review only what's doubtful", "A person resolves the exceptions flagged by the reconciliation, not every receipt."],
];

export default async function Home() {
  const supabase = createServerSupabase(await cookies());
  const { data } = await supabase.auth.getUser();
  if (data.user) redirect("/runs");

  return (
    <div
      className="relative flex min-h-screen flex-col overflow-hidden text-slate-300"
      style={{
        backgroundImage:
          "radial-gradient(ellipse 70% 45% at 50% -10%, rgba(52,211,153,0.10), transparent), linear-gradient(165deg, #080c0a 0%, #0d1812 55%, #0f1d15 100%)",
      }}
    >
      <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-emerald-400 text-sm font-bold text-emerald-950">
            L
          </div>
          <span className="text-sm font-semibold text-white">Ledgerline</span>
        </div>
        <div className="flex items-center gap-4">
          <span className="hidden items-center gap-1.5 text-xs font-medium tracking-wide text-slate-500 uppercase sm:flex">
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" aria-hidden="true" />
            Access by invitation
          </span>
          <Button render={<Link href="/login" />} className="bg-emerald-400 px-5 text-emerald-950 hover:bg-emerald-300">
            Sign in
          </Button>
        </div>
      </header>

      <main className="relative mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center px-6 py-16">
        <p className="text-xs font-medium tracking-wide text-emerald-400 uppercase">Kestrel Industries</p>
        <h1 className="mt-5 max-w-3xl font-[family-name:var(--font-fraunces)] text-5xl leading-[1.08] font-medium text-balance text-white sm:text-7xl">
          Three sources, one{" "}
          <span className="underline decoration-emerald-400 decoration-4 underline-offset-8">proven</span>
          <br />
          <span className="text-emerald-300 italic">reconciliation.</span>
        </h1>
        <p className="mt-7 max-w-xl text-base text-slate-400">
          The handwritten receiving register, the ERP&apos;s goods-received notes, and the storekeeper&apos;s sheet
          — read automatically, reconciled three ways, and proven complete. No guessing, nothing missed.
        </p>
        <div className="mt-9 flex items-center gap-4">
          <Button
            render={<Link href="/login" />}
            className="group gap-2 bg-emerald-400 px-6 py-5 text-base text-emerald-950 hover:bg-emerald-300"
          >
            Sign in to reconcile this month
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
          </Button>
          <span className="text-xs text-slate-500">No guesswork. Every figure traced.</span>
        </div>

        <div className="mt-24 grid gap-8 border-t border-white/10 pt-10 sm:grid-cols-3">
          {VALUE_PROPS.map(([n, title, body]) => (
            <div key={n}>
              <p className="text-xs font-medium tracking-wide text-emerald-400">{n}</p>
              <p className="mt-2 text-sm font-semibold text-white">{title}</p>
              <p className="mt-1 text-sm text-slate-500">{body}</p>
            </div>
          ))}
        </div>
      </main>

      <footer className="relative mx-auto w-full max-w-5xl px-6 py-8">
        <p className="text-[11px] tracking-wide text-slate-600 uppercase">Kestrel Industries · Internal system</p>
      </footer>
    </div>
  );
}
