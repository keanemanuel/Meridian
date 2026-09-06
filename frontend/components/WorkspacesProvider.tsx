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

type WorkspacesContextValue = {
  workspaces: WorkspaceMeta[];
  groups: string[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  create: (name: string, group: string) => Promise<WorkspaceMeta>;
};

const WorkspacesContext = createContext<WorkspacesContextValue | null>(null);

export function WorkspacesProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<WorkspaceMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await api.listWorkspaces();
      setWorkspaces(list);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
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
    async (name: string, group: string) => {
      const created = await api.createWorkspace(name, group);
      await refresh();
      return created;
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
    () => ({ workspaces, groups, loading, error, refresh, create }),
    [workspaces, groups, loading, error, refresh, create],
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
