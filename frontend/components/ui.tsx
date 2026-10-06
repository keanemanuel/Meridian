"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
      aria-hidden="true"
    />
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
  loading?: boolean;
  children: ReactNode;
};

const VARIANTS: Record<NonNullable<ButtonProps["variant"]>, string> = {
  // Solid purple with a hard offset shadow; pressing in on hover.
  primary:
    "border-purple-deep bg-purple-vivid text-white shadow-hard enabled:hover:translate-x-0.5 enabled:hover:translate-y-0.5 enabled:hover:shadow-hard-sm enabled:active:translate-x-1 enabled:active:translate-y-1 enabled:active:shadow-none",
  secondary:
    "border-purple-vivid bg-transparent text-purple-vivid enabled:hover:bg-purple-wash",
  danger:
    "border-danger bg-transparent text-danger enabled:hover:bg-danger-wash",
  ghost:
    "border-transparent bg-transparent text-purple-vivid enabled:hover:bg-purple-wash",
};

export function Button({
  variant = "secondary",
  loading = false,
  disabled,
  children,
  className = "",
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={`type-label inline-flex items-center justify-center gap-2 border-2 px-4 pb-2 pt-2.5 text-xs leading-none transition-[transform,box-shadow,background-color] duration-100 disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "danger" | "purple" | "pink" | "blue";
}) {
  const tones = {
    neutral: "border-accent-grey bg-paper-sunk text-ink-soft",
    danger: "border-danger bg-danger-wash text-danger",
    purple: "border-purple-vivid bg-purple-wash text-purple-vivid",
    pink: "border-accent-pink bg-accent-pink-wash text-accent-pink-ink",
    blue: "border-accent-blue bg-accent-blue-wash text-accent-blue",
  };
  return (
    <span
      className={`type-label inline-flex items-center border px-2 pb-0.5 pt-1 text-[11px] leading-none ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/** Compact search input for the schedule views. Controlled; the parent holds
 * the query and decides what to highlight. `type="search"` gives a native
 * clear affordance, and clearing it fires onChange("") like any other edit. */
export function SearchBar({
  value,
  onChange,
  placeholder = "Search by applicant or division…",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      type="search"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={placeholder}
      className="input mb-3 w-64 max-w-full"
    />
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="card overflow-hidden">
      <div className="filmstrip h-7" aria-hidden="true" />
      <div className="px-6 pb-2 pt-8 text-center">
        <p className="section-title">{title}</p>
        {hint && <p className="mt-2 text-sm text-ink-muted">{hint}</p>}
      </div>
      <div className="halftone h-12" aria-hidden="true" />
    </div>
  );
}

/** A row of tabs. Each is a typewriter label over a thick bar; the active
 * tab's bar is filled, the rest stay grey. */
export function TabBar({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap gap-x-3 gap-y-2">{children}</div>;
}

export function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      className={`tab inline-flex items-center ${active ? "tab-active" : ""}`}
    >
      {children}
    </button>
  );
}
