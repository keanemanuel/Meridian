"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { ApiError } from "@/lib/api";

type ToastKind = "success" | "error" | "info";

type Toast = {
  id: number;
  kind: ToastKind;
  message: string;
  /** Audit/validation failures carry a list of specific reasons (E-12, FR-64). */
  details?: string[];
  /** Rendered as a clickable link under the message. The Sheets export opens
   * its result in a new tab, but that call sits after an await and so is
   * routinely popup-blocked; the link is how the user still gets there. */
  link?: { href: string; label: string };
  /** kind + message + details, so a repeat of the same notification updates
   * the existing toast instead of stacking a fresh copy. */
  signature: string;
  /** How many times this exact notification has fired while still on screen.
   * Shown as a small "xN" so a recurring error reads as recurring, not as a
   * pile of identical toasts. */
  count: number;
};

export type ToastLink = { href: string; label: string };

type ToastContextValue = {
  success: (message: string, details?: string[], link?: ToastLink) => void;
  error: (message: string, details?: string[]) => void;
  info: (message: string, details?: string[]) => void;
  /** Renders an ApiError with its issue list intact. */
  fromError: (err: unknown, fallback?: string) => void;
};

const ToastContext = createContext<ToastContextValue | null>(null);

const STYLES: Record<ToastKind, string> = {
  success: "border-green-200 bg-green-50 text-green-800",
  error: "border-red-200 bg-red-50 text-red-700",
  info: "border-neutral-200 bg-white text-neutral-700",
};

const AUTO_DISMISS_MS = 5000;

/** Identity of a notification for de-duplication. `::` is a fine separator
 * here — a false match would only ever merge two genuinely identical toasts. */
function signatureOf(
  kind: ToastKind,
  message: string,
  details?: string[],
): string {
  return [kind, message, ...(details ?? [])].join("::");
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  /** Auto-dismiss timers, keyed by signature so a repeat resets the timer of
   * the toast already on screen rather than leaking a second one. */
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const clearTimer = useCallback((signature: string) => {
    const t = timers.current.get(signature);
    if (t) {
      clearTimeout(t);
      timers.current.delete(signature);
    }
  }, []);

  const dismiss = useCallback(
    (id: number) => {
      setToasts((prev) => {
        const gone = prev.find((t) => t.id === id);
        if (gone) clearTimer(gone.signature);
        return prev.filter((t) => t.id !== id);
      });
    },
    [clearTimer],
  );

  /** (Re)arm the auto-dismiss for a transient toast. Clears any existing
   * timer for the signature first, so calling it again on a repeat just
   * pushes the deadline out — and a double-invoke never leaves one dangling. */
  const armDismiss = useCallback(
    (id: number, signature: string) => {
      clearTimer(signature);
      timers.current.set(
        signature,
        setTimeout(() => {
          timers.current.delete(signature);
          dismiss(id);
        }, AUTO_DISMISS_MS),
      );
    },
    [clearTimer, dismiss],
  );

  const push = useCallback(
    (kind: ToastKind, message: string, details?: string[], link?: ToastLink) => {
      const signature = signatureOf(kind, message, details);
      // Errors stay put — they usually carry a reason worth reading. So does
      // anything carrying a link, which is there to be clicked.
      const transient = kind !== "error" && !link;

      setToasts((prev) => {
        const existing = prev.find((t) => t.signature === signature);
        if (existing) {
          // Same notification, still on screen: bump its count, don't stack.
          if (transient) armDismiss(existing.id, signature);
          return prev.map((t) =>
            t.id === existing.id ? { ...t, count: t.count + 1 } : t,
          );
        }
        const id = Date.now() + Math.random();
        if (transient) armDismiss(id, signature);
        return [
          ...prev,
          { id, kind, message, details, link, signature, count: 1 },
        ];
      });
    },
    [armDismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach((t) => clearTimeout(t));
      pending.clear();
    };
  }, []);

  const value = useMemo<ToastContextValue>(
    () => ({
      success: (m, d, link) => push("success", m, d, link),
      error: (m, d) => push("error", m, d),
      info: (m, d) => push("info", m, d),
      fromError: (err, fallback = "Something went wrong.") => {
        const e = err as Partial<ApiError>;
        const details = (e?.issues ?? []).map((i) =>
          [i.applicant_id, i.code, i.message].filter(Boolean).join(" · "),
        );
        push("error", e?.message ?? fallback, details.length ? details : undefined);
      },
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-96 flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto rounded border px-4 py-3 text-sm shadow-sm ${STYLES[t.kind]}`}
          >
            <div className="flex items-start gap-3">
              <p className="flex-1 leading-snug">
                {t.message}
                {t.count > 1 && (
                  <span className="ml-1.5 rounded-full bg-black/10 px-1.5 text-xs font-semibold tabular-nums">
                    &times;{t.count}
                  </span>
                )}
              </p>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                className="-m-1.5 shrink-0 rounded p-1.5 text-xs leading-none opacity-60 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-current"
                aria-label="Dismiss notification"
              >
                &times;
              </button>
            </div>
            {t.link && (
              <a
                href={t.link.href}
                target="_blank"
                rel="noreferrer"
                className="mt-2 inline-block break-all font-medium underline underline-offset-2"
              >
                {t.link.label}
              </a>
            )}
            {t.details && t.details.length > 0 && (
              <ul className="mt-2 max-h-40 list-disc overflow-y-auto pl-4 text-xs opacity-90">
                {t.details.slice(0, 25).map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
                {t.details.length > 25 && (
                  <li>&hellip;and {t.details.length - 25} more</li>
                )}
              </ul>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
