"use client";

// 006-web T020 (FR-003, research R3): app-wide client state.
// - QueryClientProvider: TanStack Query for every client read/mutation (no other state library).
// - MeProvider: calls EP-02 once per page session on signed-in pages; 403 → /forbidden; exposes
//   `useMe()` with the person and their role permissions (lib/me.ts). The API stays the authority.

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, api } from "@/lib/api";
import { isSignedOutPath, permissionsFor, toSessionPerson, type Permissions, type SessionPerson } from "@/lib/me";

/** TanStack Query key of the EP-02 answer. The login page should `removeQueries` it after a
 * sign-in so a different person never sees the previous one's role. */
export const ME_QUERY_KEY = ["me"] as const;

export type MeState = {
  /** "idle" on pages that never ask (sign-in, /forbidden, test pages). */
  status: "idle" | "loading" | "ready" | "error";
  person: SessionPerson | null;
  error: ApiError | null;
  /** Role permissions; all false until EP-02 answers. */
  can: Permissions;
};

const NO_PERMISSIONS = permissionsFor(null);
const MeContext = createContext<MeState>({
  status: "idle",
  person: null,
  error: null,
  can: NO_PERMISSIONS,
});

export function useMe(): MeState {
  return useContext(MeContext);
}

function asksWhoAmI(pathname: string): boolean {
  return !isSignedOutPath(pathname) && pathname !== "/forbidden";
}

function MeProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const enabled = asksWhoAmI(pathname);

  const query = useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: () => api.get("/me"),
    enabled,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });

  const error = query.error instanceof ApiError ? query.error : null;

  useEffect(() => {
    if (enabled && error?.status === 403) router.replace("/forbidden");
  }, [enabled, error, router]);

  const value = useMemo<MeState>(() => {
    if (!enabled) return { status: "idle", person: null, error: null, can: NO_PERMISSIONS };
    if (query.data) {
      const person = toSessionPerson(query.data);
      return { status: "ready", person, error: null, can: person.can };
    }
    if (query.isError) {
      return {
        status: "error",
        person: null,
        error: error ?? new ApiError({ status: 0, code: "BAD_RESPONSE", message: String(query.error) }),
        can: NO_PERMISSIONS,
      };
    }
    return { status: "loading", person: null, error: null, can: NO_PERMISSIONS };
  }, [enabled, query.data, query.isError, query.error, error]);

  return <MeContext.Provider value={value}>{children}</MeContext.Provider>;
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 4xx answers are final (and 401 already redirects); retry network/5xx once.
        retry: (count, err) =>
          !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 1,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <MeProvider>{children}</MeProvider>
    </QueryClientProvider>
  );
}
