import type { Metadata } from "next";
import { Bricolage_Grotesque, Figtree, Geist_Mono } from "next/font/google";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { Providers } from "./providers";

// Ledgerline's typographic system (2026 redesign): Figtree for every UI/body string, Bricolage
// Grotesque for headlines (landing, login, dashboard, page/card titles) — both replace the
// previous Geist/Fraunces pairing everywhere, not just the redesigned pages, so the whole app
// reads as one product.
const figtree = Figtree({
  variable: "--font-figtree",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Ledgerline",
  description: "GRN reconciliation across register, ERP and store sheet",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${figtree.variable} ${geistMono.variable} ${bricolage.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        {/* 006-web T020 (NFR-09): first tab stop jumps past navigation to the screen. */}
        <a href="#main" className="skip-link">
          Skip to main content
        </a>
        <Providers>
          <AppShell>
            <main id="main" tabIndex={-1} className="flex flex-1 flex-col">
              {children}
            </main>
          </AppShell>
        </Providers>
      </body>
    </html>
  );
}
