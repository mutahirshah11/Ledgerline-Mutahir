import {
  ArrowDown,
  ArrowRight,
  Eye,
  Flag,
  GitCompare,
  History,
  Landmark,
  Lock,
  PlayCircle,
  Receipt,
  ShieldCheck,
  UploadCloud,
  Warehouse,
} from "lucide-react";
import { cookies } from "next/headers";
import Link from "next/link";
import { redirect } from "next/navigation";

import { Button } from "@/components/ui/button";
import { createServerSupabase } from "@/lib/supabase";

// Public marketing landing (2026 redesign, Claude-design handoff "Landing" screen): a signed-in
// visitor is sent straight to /runs, same as before; a signed-out visitor sees this page. "/" is
// in lib/me.ts SIGNED_OUT_PREFIXES so proxy.ts lets it through unauthenticated.

const HOW_IT_WORKS: ReadonlyArray<{
  n: string;
  icon: typeof UploadCloud;
  tint: string;
  title: string;
  body: string;
}> = [
  {
    n: "01",
    icon: UploadCloud,
    tint: "bg-primary-soft",
    title: "Add the month's records",
    body: "Photograph the handwritten register, connect your ERP once, and upload the storekeeper's sheet.",
  },
  {
    n: "02",
    icon: GitCompare,
    tint: "bg-success-soft",
    title: "Ledgerline lines them up",
    body: "Every delivery is compared on item, quantity, and date. Lines that agree are cleared automatically.",
  },
  {
    n: "03",
    icon: Flag,
    tint: "bg-warning-soft",
    title: "You decide the odd ones",
    body: "Mismatches are shown side by side with the odd one out highlighted. Pick the right value and close the month.",
  },
];

const WHO_ITS_FOR: ReadonlyArray<{ icon: typeof Landmark; title: string; body: string }> = [
  {
    icon: Landmark,
    title: "Controllers",
    body: "Close the month with a plain-English answer to \"did everything arrive the way it should have?\"",
  },
  {
    icon: Receipt,
    title: "AP reviewers",
    body: "Stop cross-checking receipts by hand — only the lines that actually disagree reach you.",
  },
  {
    icon: Warehouse,
    title: "Warehouse leads",
    body: "A clear record of what was received, provable against the register and the ERP.",
  },
];

const TRUST_ITEMS: ReadonlyArray<{ icon: typeof Lock; text: string }> = [
  { icon: Lock, text: "Encrypted in transit and at rest" },
  { icon: Eye, text: "Every change is visible to your team" },
  { icon: History, text: "Full audit trail, always" },
];

export default async function Home() {
  const supabase = createServerSupabase(await cookies());
  const { data } = await supabase.auth.getUser();
  if (data.user) redirect("/runs");

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b border-border bg-background/90 backdrop-blur">
        <nav
          aria-label="Primary"
          className="mx-auto flex h-16 w-full max-w-[1160px] items-center justify-between px-6"
        >
          <Link href="/" className="flex items-center gap-2.5">
            <Logo />
            <span className="font-heading text-lg font-bold text-foreground">Ledgerline</span>
          </Link>
          <div className="hidden items-center gap-7 text-sm font-medium text-ink-2 sm:flex">
            <a href="#how" className="hover:text-foreground">
              How it works
            </a>
            <a href="#who" className="hover:text-foreground">
              Who it&apos;s for
            </a>
            <a href="#security" className="hover:text-foreground">
              Security
            </a>
          </div>
          <div className="flex items-center gap-3">
            <Button render={<Link href="/login" />} variant="secondary" className="border border-border-strong">
              Sign in
            </Button>
            <Button render={<Link href="/login" />}>Book a demo</Button>
          </div>
        </nav>
      </header>

      <main className="flex-1">
        <section className="mx-auto grid w-full max-w-[1160px] gap-14 px-6 py-20 lg:grid-cols-2 lg:items-center lg:gap-10">
          <div>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border-strong bg-card px-3 py-1.5 text-xs font-medium text-ink-2">
              <ShieldCheck className="h-3.5 w-3.5 text-primary" aria-hidden="true" />
              Month-end receiving checks for finance teams
            </span>
            <h1 className="mt-5 max-w-xl font-heading text-5xl leading-[1.08] font-bold tracking-[-0.03em] text-balance sm:text-[60px]">
              Every delivery, checked <span className="text-primary">three ways.</span>
            </h1>
            <p className="mt-6 max-w-lg text-lg leading-[1.55] text-ink-2">
              Ledgerline compares your handwritten receiving register, ERP goods-received notes, and
              the storekeeper&apos;s sheet every month. Lines that agree are cleared. You only look
              at the few that don&apos;t.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-4">
              <Button render={<Link href="/login" />} size="lg" className="group h-12 gap-2 px-6 text-[15px] font-semibold">
                Sign in to your workspace
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
              </Button>
              <a
                href="#how"
                className="inline-flex h-12 items-center gap-2 rounded-lg border border-border-strong bg-card px-5 text-[15px] font-semibold text-foreground hover:bg-muted"
              >
                <PlayCircle className="h-4 w-4 text-primary" aria-hidden="true" />
                See how it works
              </a>
            </div>
          </div>

          <div className="rounded-[22px] bg-card p-6 shadow-[0_30px_60px_-30px_rgba(13,42,44,0.25)]">
            <div className="grid grid-cols-3 gap-3">
              {[
                ["Receiving register", "415 lines"],
                ["ERP goods-received notes", "415 lines"],
                ["Storekeeper's sheet", "415 lines"],
              ].map(([name, lines]) => (
                <div key={name} className="rounded-xl bg-muted p-3">
                  <p className="text-xs font-semibold text-foreground">{name}</p>
                  <p className="mt-2 text-xs font-medium text-success-ink">✓ {lines}</p>
                </div>
              ))}
            </div>
            <ArrowDown className="mx-auto mt-4 h-5 w-5 text-faint" aria-hidden="true" />
            <div className="mt-4 rounded-2xl bg-primary p-5 text-white">
              <p className="text-xs font-medium text-primary-tint-text uppercase">September result</p>
              <p className="mt-1 font-heading text-2xl font-bold">
                412 match. <span className="text-success">3 need you.</span>
              </p>
              <div className="mt-3 flex items-center gap-3 text-xs text-primary-tint-text">
                <span className="flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-[2px] bg-success" /> Matched
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-[2px] bg-warning" /> Needs you
                </span>
              </div>
              <div className="mt-3 grid grid-cols-[repeat(15,minmax(0,1fr))] gap-[3px]">
                {Array.from({ length: 45 }, (_, i) => (
                  <span
                    key={i}
                    className="aspect-square rounded-[2.5px]"
                    style={{ background: [6, 19, 33].includes(i) ? "var(--warning)" : "var(--success)" }}
                  />
                ))}
              </div>
            </div>
          </div>
        </section>

        <section id="how" className="border-y border-border bg-card py-20">
          <div className="mx-auto w-full max-w-[1160px] px-6">
            <p className="text-sm font-semibold text-primary">How it works</p>
            <h2 className="mt-2 max-w-xl font-heading text-[38px] leading-[1.1] font-bold tracking-[-0.03em]">
              Three steps, and the boring part is done for you.
            </h2>
            <div className="mt-10 grid gap-6 sm:grid-cols-3">
              {HOW_IT_WORKS.map(({ n, icon: Icon, tint, title, body }) => (
                <div key={n} className={`rounded-2xl ${tint} p-7`}>
                  <div className="flex items-start justify-between">
                    <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-card">
                      <Icon className="h-5 w-5 text-primary" aria-hidden="true" />
                    </span>
                    <span className="text-sm font-bold text-ink-2">{n}</span>
                  </div>
                  <h3 className="mt-6 font-heading text-lg font-bold">{title}</h3>
                  <p className="mt-2 text-sm leading-[1.5] text-ink-2">{body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="who" className="py-20">
          <div className="mx-auto w-full max-w-[1160px] px-6">
            <p className="text-sm font-semibold text-primary">Who it&apos;s for</p>
            <h2 className="mt-2 max-w-xl font-heading text-[38px] leading-[1.1] font-bold tracking-[-0.03em]">
              Built for the people who sign off on month-end.
            </h2>
            <div className="mt-10 grid gap-6 sm:grid-cols-3">
              {WHO_ITS_FOR.map(({ icon: Icon, title, body }) => (
                <div key={title} className="rounded-2xl border border-border bg-card p-7">
                  <span className="flex h-11 w-11 items-center justify-center rounded-full bg-primary-soft">
                    <Icon className="h-5 w-5 text-primary" aria-hidden="true" />
                  </span>
                  <h3 className="mt-6 font-heading text-lg font-bold">{title}</h3>
                  <p className="mt-2 text-sm leading-[1.5] text-ink-2">{body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="security" className="pb-20">
          <div className="mx-auto w-full max-w-[1160px] px-6">
            <div className="grid gap-10 rounded-[26px] bg-ink p-10 text-white sm:p-14 lg:grid-cols-2 lg:items-center">
              <div>
                <h2 className="max-w-sm font-heading text-[32px] leading-[1.15] font-bold tracking-[-0.03em]">
                  Ready for this month&apos;s close?
                </h2>
                <Button
                  render={<Link href="/login" aria-label="Sign in to your workspace" />}
                  size="lg"
                  className="mt-6 h-12 bg-success px-6 text-[15px] font-semibold text-success-ink hover:bg-success/90"
                >
                  Sign in
                </Button>
              </div>
              <div className="flex flex-col gap-4">
                {TRUST_ITEMS.map(({ icon: Icon, text }) => (
                  <div key={text} className="flex items-center gap-3 text-sm text-white/80">
                    <Icon className="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
                    {text}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto w-full max-w-[1160px] px-6 py-8 text-xs text-muted-foreground">
          © 2026 Ledgerline · Privacy · Terms · Status
        </div>
      </footer>
    </div>
  );
}

function Logo() {
  return (
    <span className="flex h-7 w-7 shrink-0 flex-col items-start justify-center gap-[3px] rounded-[8px] bg-primary px-[7px]">
      <span className="h-[2.5px] w-full rounded-full bg-white" />
      <span className="h-[2.5px] w-full rounded-full bg-white" />
      <span className="h-[2.5px] w-[60%] rounded-full bg-success" />
    </span>
  );
}
