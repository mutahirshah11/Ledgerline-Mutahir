// 006-web T048: SCR-06 `/runs/[id]/review` (contracts/screens.md). The queue itself is a client
// component (EP-14/15/16 from the browser); this server page only resolves the route param
// (a Promise in Next 16).

import { ReviewQueue } from "./ReviewQueue";

export const metadata = { title: "Review queue · Ledgerline" };

export default async function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ReviewQueue runId={id} />;
}
