// contracts/components.md "Loading, Empty, ErrorState — WEB-AC6"; ui-copy.md §7.
//
// ErrorState takes a minimal local shape rather than importing `ApiError` from `lib/api.ts`
// (owned by another 006-web agent working concurrently) so this file has no dependency on it;
// any real `ApiError` the app produces is assignable to this shape.
export type ApiErrorLike = {
  status: number;
  code?: string | null;
  message?: string | null;
};

export function Loading() {
  return <p role="status">Loading…</p>;
}

export type EmptyProps = {
  text: string;
  action?: React.ReactNode;
};

export function Empty({ text, action }: EmptyProps) {
  return (
    <div>
      <p>{text}</p>
      {action}
    </div>
  );
}

// ui-copy.md §7 "Common to every data screen" (contracts/screens.md): 401 is handled by a
// redirect to /login elsewhere (middleware/providers), not shown as text here; 403 role ->
// "Your role cannot do this"; 404 -> "Run not found"; network/5xx -> "Ledgerline is not reachable
// right now"; anything else falls back to the API's own verbatim message (never composed here,
// F-09.R2), or the same unreachable text if there is no message at all.
export function errorStateText(error: ApiErrorLike): string {
  if (error.status === 404) return "Run not found";
  if (error.status === 403 && error.code === "NOT_ALLOWED") return "Your role cannot do this";
  if (error.status === 0 || error.status >= 500) return "Ledgerline is not reachable right now";
  return error.message || "Ledgerline is not reachable right now";
}

export type ErrorStateProps = {
  error: ApiErrorLike;
};

export function ErrorState({ error }: ErrorStateProps) {
  return <p role="alert">{errorStateText(error)}</p>;
}
