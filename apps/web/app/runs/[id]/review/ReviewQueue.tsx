"use client";

// 006-web T048: SCR-06 review queue (contracts/screens.md SCR-06; FR-025..FR-030, FR-036).
// - EP-14 `?status=OPEN` in the API's order (F-08.R2); one card at a time; progress
//   "{left} of {total} left" where total is the most open items seen since the queue opened.
// - EP-16 per page shown (5-minute link, cached 4 minutes), renewed once on an image error.
// - EP-15, then the run from its answer and a fresh EP-14; the next card shows. A 409 keeps the
//   decided card on screen with the first decision while the queue refreshes behind it.
// - Read-only (no inputs, no actions, reason stated) for a FINALISED run or a role without
//   resolve (OPS); the API stays the authority.
// EP-05 is read once for the run status (FINALISED, and READY when the queue empties).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useCallback, useMemo, useState } from "react";

import { useMe } from "@/app/providers";
import { ReviewCard, toReviewCardModel, type ApiReviewCard, type ResolveInput } from "@/components/ReviewCard";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import { Button } from "@/components/ui/button";
import { ErrorState, Loading } from "@/components/states";
import { api, ApiError } from "@/lib/api";

const FINALISED_TEXT = "This run is finalised and read-only.";
const ROLE_TEXT = "Your role can view but not decide.";
const LINK_STALE_MS = 4 * 60 * 1000; // EP-16 links live 5 minutes

export function ReviewQueue({ runId }: { runId: string }) {
  const me = useMe();
  const queryClient = useQueryClient();
  const [total, setTotal] = useState(0);
  const [decisions, setDecisions] = useState(0);
  const [pinned, setPinned] = useState<ApiReviewCard | null>(null);

  const runKey = useMemo(() => ["review", runId, "run"] as const, [runId]);
  const runQ = useQuery({
    queryKey: runKey,
    queryFn: ({ signal }) => api.get("/runs/{id}", { path: { id: runId }, signal }),
  });
  const listQ = useQuery({
    queryKey: ["review", runId, "open-items"],
    queryFn: async ({ signal }) => {
      const items = await api.get("/runs/{id}/review-items", {
        path: { id: runId },
        query: { status: "OPEN" },
        signal,
      });
      setTotal((t) => Math.max(t, items.length));
      return items;
    },
  });

  const current: ApiReviewCard | null = pinned ?? listQ.data?.[0] ?? null;
  const pageNo =
    current && typeof current.context === "object" && "page_no" in current.context ? current.context.page_no : null;
  const linkKey = useMemo(() => ["review", runId, "page-image", pageNo] as const, [runId, pageNo]);
  const linkQ = useQuery({
    queryKey: linkKey,
    queryFn: ({ signal }) =>
      api.get("/runs/{id}/pages/{page_no}/image", { path: { id: runId, page_no: pageNo as number }, signal }),
    enabled: pageNo !== null,
    staleTime: LINK_STALE_MS,
  });

  const refetchList = listQ.refetch;
  const onAction = useCallback(
    async (input: ResolveInput) => {
      if (!current) return;
      try {
        const res = await api.post("/review-items/{item_id}/resolve", {
          path: { item_id: current.id },
          body: input,
        });
        queryClient.setQueryData(runKey, res.run);
        await refetchList();
        setDecisions((d) => d + 1);
      } catch (err) {
        if (err instanceof ApiError && err.status === 409 && err.code === "ITEM_ALREADY_RESOLVED") {
          setPinned(current);
          void refetchList();
        }
        throw err;
      }
    },
    [current, queryClient, runKey, refetchList],
  );

  const onImageError = useCallback(async () => {
    if (pageNo === null) throw new Error("no page");
    const link = await api.get("/runs/{id}/pages/{page_no}/image", { path: { id: runId, page_no: pageNo } });
    queryClient.setQueryData(linkKey, link);
    return link.signed_url;
  }, [pageNo, queryClient, linkKey, runId]);

  const run = runQ.data;
  const finalised = run?.status === "FINALISED";
  const readOnly = finalised || !run || !me.can.resolve;
  const readOnlyReason = finalised ? FINALISED_TEXT : me.status === "ready" && !me.can.resolve ? ROLE_TEXT : undefined;
  const items = listQ.data;
  const left = items?.length ?? 0;
  const error = listQ.error ?? runQ.error;

  return (
    <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-4 px-3 py-4 sm:px-6">
      <p className="text-sm">
        <Link href={`/runs/${runId}`} className="underline underline-offset-2">
          Run overview
        </Link>
      </p>
      <h1 className="text-xl font-semibold">Review queue</h1>

      {error ? (
        <ErrorState error={error instanceof ApiError ? error : { status: 0 }} />
      ) : !items ? (
        <Loading />
      ) : (
        <>
          {total > 0 ? (
            <p data-testid="review-progress" className="text-sm font-medium">
              {left} of {total} left
            </p>
          ) : null}

          {current ? (
            <>
              <ReviewCard
                key={`${current.id}:${decisions}`}
                item={toReviewCardModel(current, linkQ.data ?? null)}
                readOnly={readOnly}
                readOnlyReason={readOnlyReason}
                onAction={onAction}
                onImageError={onImageError}
              />
              {pinned ? (
                <div>
                  <Button type="button" variant="outline" onClick={() => setPinned(null)}>
                    Next item
                  </Button>
                </div>
              ) : null}
            </>
          ) : (
            <div className="flex flex-col gap-2">
              <p>Nothing left to review.</p>
              {readOnlyReason ? <p className="text-sm text-muted-foreground">{readOnlyReason}</p> : null}
              {run ? (
                <p className="text-sm">
                  Run status: <RunStatusBadge status={run.status} failedStage={run.failed_stage} />
                </p>
              ) : null}
              <p>
                <Link href={`/runs/${runId}`} className="underline underline-offset-2">
                  Back to overview
                </Link>
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
