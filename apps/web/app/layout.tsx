import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { Providers } from "./providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
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
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
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
