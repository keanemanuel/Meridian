"use client";

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

/** The "Are you verified?" gate. On a deployment that disguises applicant
 * names (REVEAL_PASSWORD set on the API), everyone sees CAND0001-style aliases
 * until they enter the password; the API does the disguising, this only holds
 * the token and tells the views when to refetch. */
type RevealContextValue = {
  /** False on a deployment that shows real names to everyone — the control
   * is hidden there. */
  maskingEnabled: boolean;
  revealed: boolean;
  /** Bumped on every unlock/lock, so views refetch under the new identity. */
  epoch: number;
  unlock: (password: string) => Promise<void>;
  lock: () => void;
};

const RevealContext = createContext<RevealContextValue | null>(null);

export function RevealProvider({ children }: { children: ReactNode }) {
  const [maskingEnabled, setMaskingEnabled] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    void (async () => {
      try {
        const status = await api.revealStatus();
        setMaskingEnabled(status.masking_enabled);
        setRevealed(status.masking_enabled && status.revealed);
        // A stored token the API no longer honours (expired, or the password
        // was rotated) is dead weight; everything fetched with it already
        // came back disguised, so there is nothing to refetch.
        if (!status.revealed) clearRevealToken();
      } catch {
        /* API unreachable — the workspace list surfaces that error */
      }
    })();
  }, []);

  const lock = useCallback(() => {
    clearRevealToken();
    setRevealed(false);
    setEpoch((n) => n + 1);
  }, []);

  const unlock = useCallback(async (password: string) => {
    const { token, expires_at } = await api.reveal(password);
    storeRevealToken(token, expires_at);
    setRevealed(true);
    setEpoch((n) => n + 1);
  }, []);

  // Fall back to aliases the moment the token lapses, rather than leaving a
  // "real names" badge over data the API has quietly started disguising again.
  useEffect(() => {
    if (!revealed) return;
    const expiresAt = getRevealExpiry();
    if (expiresAt === null) return;
    const timer = setTimeout(lock, Math.max(0, expiresAt * 1000 - Date.now()));
    return () => clearTimeout(timer);
  }, [revealed, epoch, lock]);

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
