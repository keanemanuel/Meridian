"use client";

import { usePathname } from "next/navigation";
import {
  createContext,
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ApiError, api } from "@/lib/api";
import {
  clearRevealToken,
  getRevealExpiry,
  storeRevealToken,
} from "@/lib/reveal";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

/** The "Are you verified?" gate. Masking is per-workspace (src/api/privacy.py):
 * most workspaces always show real names, and only the one(s) the deployment
 * names (e.g. "IFF 2026-27 Registration") disguise them as CAND0001-style
 * aliases until the password is entered. The API does the disguising; this
 * only tracks which workspace is open, holds its token, and tells the views
 * when to refetch. */
type RevealContextValue = {
  /** False outside a workspace, or inside one that shows real names to
   * everyone — the control is hidden there. */
  maskingEnabled: boolean;
  revealed: boolean;
  /** Bumped on every unlock/lock, so views refetch under the new identity. */
  epoch: number;
  unlock: (password: string) => Promise<void>;
  lock: () => void;
};

const RevealContext = createContext<RevealContextValue | null>(null);

/** Pulls the workspace id back out of `/workspace/<id>` / `/workspace/<id>/...`.
 * Null everywhere else (home, the workspace list) — there's no workspace to
 * check masking for there. */
function workspaceIdFromPathname(pathname: string | null): string | null {
  if (!pathname) return null;
  const match = /^\/workspace\/([^/]+)/.exec(pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

export function RevealProvider({ children }: { children: ReactNode }) {
  const workspaceId = workspaceIdFromPathname(usePathname());
  const [fetchedMasking, setFetchedMasking] = useState(false);
  const [fetchedRevealed, setFetchedRevealed] = useState(false);
  const [epoch, setEpoch] = useState(0);

  // Outside a workspace (home, the workspace list) there's nothing to
  // unlock, so these are derived to false at render time rather than reset
  // via a synchronous setState in the effect below — that avoids carrying
  // over a previous workspace's masking state without an extra render.
  const maskingEnabled = workspaceId ? fetchedMasking : false;
  const revealed = workspaceId ? fetchedRevealed : false;

  useEffect(() => {
    if (!workspaceId) return;
    void (async () => {
      try {
        const status = await api.revealStatus(workspaceId);
        setFetchedMasking(status.masking_enabled);
        setFetchedRevealed(status.masking_enabled && status.revealed);
        // A stored token the API no longer honours (expired, or the password
        // was rotated) is dead weight; everything fetched with it already
        // came back disguised, so there is nothing to refetch.
        if (!status.revealed) clearRevealToken(workspaceId);
      } catch {
        /* API unreachable — the workspace list surfaces that error */
      }
    })();
  }, [workspaceId]);

  const lock = useCallback(() => {
    if (!workspaceId) return;
    clearRevealToken(workspaceId);
    setFetchedRevealed(false);
    setEpoch((n) => n + 1);
  }, [workspaceId]);

  const unlock = useCallback(
    async (password: string) => {
      if (!workspaceId) return;
      const { token, expires_at } = await api.reveal(workspaceId, password);
      storeRevealToken(workspaceId, token, expires_at);
      setFetchedRevealed(true);
      setEpoch((n) => n + 1);
    },
    [workspaceId],
  );

  // Fall back to aliases the moment the token lapses, rather than leaving a
  // "real names" badge over data the API has quietly started disguising again.
  useEffect(() => {
    if (!revealed || !workspaceId) return;
    const expiresAt = getRevealExpiry(workspaceId);
    if (expiresAt === null) return;
    const timer = setTimeout(lock, Math.max(0, expiresAt * 1000 - Date.now()));
    return () => clearTimeout(timer);
  }, [revealed, epoch, lock, workspaceId]);

  const value = useMemo<RevealContextValue>(
    () => ({ maskingEnabled, revealed, epoch, unlock, lock }),
    [maskingEnabled, revealed, epoch, unlock, lock],
  );

  return (
    <RevealContext.Provider value={value}>{children}</RevealContext.Provider>
  );
}

export function useReveal(): RevealContextValue {
  const ctx = useContext(RevealContext);
  if (!ctx) throw new Error("useReveal must be used inside <RevealProvider>");
  return ctx;
}

/** Remounts the page whenever names are unlocked or hidden again, so every
 * view reloads its data and no real name lingers in component state. */
export function RevealBoundary({ children }: { children: ReactNode }) {
  const { epoch } = useReveal();
  return <Fragment key={epoch}>{children}</Fragment>;
}

function VerifyModal({ onClose }: { onClose: () => void }) {
  const { unlock } = useReveal();
  const toast = useToast();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [wrong, setWrong] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setBusy(true);
    setWrong(false);
    try {
      await unlock(password);
      toast.success("Verified. Showing real names.");
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setWrong(true);
        setPassword("");
      } else {
        toast.fromError(err, "Could not verify.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Are you verified?" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-ink-muted">
          Applicant names are disguised as CAND0001, CAND0002 and so on. Enter
          the password to see real names.
        </p>
        <div>
          <label htmlFor="reveal-password" className="field-label">
            Password
          </label>
          <input
            id="reveal-password"
            type="password"
            autoFocus
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={wrong}
            className="input w-full"
          />
          {wrong && (
            <p role="alert" className="mt-1.5 text-xs text-danger">
              Wrong password.
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={busy}
            disabled={!password}
          >
            See real names
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Header control: shows whether names are disguised and opens the password
 * prompt. Renders nothing on a deployment that doesn't disguise names. */
export function RevealControl() {
  const { maskingEnabled, revealed, lock } = useReveal();
  const [asking, setAsking] = useState(false);

  if (!maskingEnabled) return null;

  const button =
    "type-label border-2 px-3 pb-1.5 pt-2 text-[11px] leading-none transition-colors focus-visible:outline-white";

  return (
    <>
      {revealed ? (
        <button
          type="button"
          onClick={lock}
          title="Go back to disguised names"
          className={`${button} border-paper bg-paper text-purple-deep hover:bg-purple-tint`}
        >
          Real names · Hide
        </button>
      ) : (
        <button
          type="button"
          onClick={() => setAsking(true)}
          title="Enter the password to see real names"
          className={`${button} border-purple-tint text-purple-tint hover:border-paper hover:text-paper`}
        >
          Names hidden · Verify
        </button>
      )}
      {asking && <VerifyModal onClose={() => setAsking(false)} />}
    </>
  );
}
