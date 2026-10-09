import { Suspense } from "react";

import { ResultsScreen } from "./ResultsScreen";

// 006-web T054: SCR-05 `/runs/[id]/results` (contracts/screens.md SCR-05, FR-023/FR-024). The
// outcome filter and GRN search live in the URL (`?outcome=`, `?grn=`) so the filtered view is a
// shareable link and survives a reload; `useSearchParams` needs a Suspense boundary in this
// Next version (same reasoning as app/login/page.tsx).

export const metadata = { title: "Results · Ledgerline" };

export default async function ResultsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Suspense>
      <ResultsScreen runId={id} />
    </Suspense>
  );
}
