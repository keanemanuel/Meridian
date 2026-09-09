"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
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

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string, details?: string[], link?: ToastLink) => {
      const id = Date.now() + Math.random();
      setToasts((prev) => [...prev, { id, kind, message, details, link }]);
      // Errors stay put — they usually carry a reason worth reading. So does
      // anything carrying a link, which is there to be clicked.
      if (kind !== "error" && !link) {
        setTimeout(() => dismiss(id), 5000);
      }
    },
    [dismiss],
  );

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
              <p className="flex-1 leading-snug">{t.message}</p>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                className="shrink-0 text-xs opacity-60 hover:opacity-100"
                aria-label="Dismiss"
              >
                ✕
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
                  <li>…and {t.details.length - 25} more</li>
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
