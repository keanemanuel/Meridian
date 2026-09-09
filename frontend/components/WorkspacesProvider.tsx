"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/lib/api";
import type { WorkspaceMeta } from "@/lib/types";

/** The two groups the sidebar always shows, in order (SPEC.md §11.1).
 * Any other group found in the data is rendered after these rather than
 * dropped — a workspace must never be invisible just because its group is
 * unexpected. */
export const FIXED_GROUPS = ["Test Environment", "IFF Submissions"] as const;

/** Groups holding live recruitment submissions. Renaming one of these
 * workspaces asks for a stronger confirmation, and deleting one is refused
 * outright (the API enforces the same rule in
 * `api/routers/workspaces.py:PROTECTED_GROUPS`). */
export const PROTECTED_GROUPS: readonly string[] = ["IFF Submissions"];

export const isProtectedGroup = (group: string) =>
  PROTECTED_GROUPS.includes(group);

type WorkspacesContextValue = {
  workspaces: WorkspaceMeta[];
  groups: string[];
  loading: boolean;
  /** True while a request is being retried through a Railway cold start —
   * the UI shows "Connecting to backend…" rather than an error. */
  connecting: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  create: (
    name: string,
    group: string,
    sheetUrl?: string,
  ) => Promise<WorkspaceMeta>;
  rename: (id: string, name: string) => Promise<WorkspaceMeta>;
  remove: (id: string) => Promise<void>;
};

const WorkspacesContext = createContext<WorkspacesContextValue | null>(null);

export function WorkspacesProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<WorkspaceMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await api.listWorkspaces({
        onRetry: () => setConnecting(true),
      });
      setWorkspaces(list);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
      setConnecting(false);
    }
  }, []);

  useEffect(() => {
    // Wrapped rather than called directly: `refresh` sets state, and the React
    // compiler lint forbids that synchronously in an effect body.
    void (async () => {
      await refresh();
    })();
  }, [refresh]);

  const create = useCallback(
    async (name: string, group: string, sheetUrl?: string) => {
      try {
        const created = await api.createWorkspace(name, group, {
          sheetUrl,
          onRetry: () => setConnecting(true),
        });
        await refresh();
        return created;
      } finally {
        setConnecting(false);
      }
    },
    [refresh],
  );

  const rename = useCallback(
    async (id: string, name: string) => {
      const updated = await api.renameWorkspace(id, name);
      await refresh();
      return updated;
    },
    [refresh],
  );

  const remove = useCallback(
    async (id: string) => {
      await api.deleteWorkspace(id);
      await refresh();
    },
    [refresh],
  );

  const groups = useMemo(() => {
    const extra = [...new Set(workspaces.map((w) => w.group))]
      .filter((g) => !FIXED_GROUPS.includes(g as (typeof FIXED_GROUPS)[number]))
      .sort();
    return [...FIXED_GROUPS, ...extra];
  }, [workspaces]);

  const value = useMemo<WorkspacesContextValue>(
    () => ({
      workspaces,
      groups,
      loading,
      connecting,
      error,
      refresh,
      create,
      rename,
      remove,
    }),
    [
      workspaces,
      groups,
      loading,
      connecting,
      error,
      refresh,
      create,
      rename,
      remove,
    ],
  );

  return (
    <WorkspacesContext.Provider value={value}>
      {children}
    </WorkspacesContext.Provider>
  );
}

export function useWorkspaces(): WorkspacesContextValue {
  const ctx = useContext(WorkspacesContext);
  if (!ctx)
    throw new Error("useWorkspaces must be used inside <WorkspacesProvider>");
  return ctx;
}
