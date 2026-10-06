"use client";

import { useEffect, type ReactNode } from "react";

export function Modal({
  title,
  onClose,
  children,
  width = "w-[28rem]",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  width?: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-purple-deep/45 p-4 pt-24"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`${width} card max-h-[70vh] max-w-full overflow-y-auto shadow-hard-lg`}
      >
        <div className="flex items-center justify-between gap-4 bg-purple-deep px-5 py-3 text-paper">
          <h2 className="type-label pt-0.5 text-sm">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-purple-tint hover:text-white focus-visible:outline-white"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
