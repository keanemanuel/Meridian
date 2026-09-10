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
  primary:
    "bg-neutral-900 text-white border-neutral-900 hover:bg-neutral-700 hover:border-neutral-700",
  secondary:
    "bg-white text-neutral-800 border-neutral-300 hover:bg-neutral-50 hover:border-neutral-400",
  danger:
    "bg-white text-red-600 border-red-300 hover:bg-red-50 hover:border-red-400",
  ghost:
    "bg-transparent text-neutral-600 border-transparent hover:bg-neutral-100",
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
      className={`inline-flex items-center justify-center gap-2 rounded-md border px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
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
  tone?: "neutral" | "red" | "green" | "amber";
}) {
  const tones = {
    neutral: "bg-neutral-100 text-neutral-600 border-neutral-200",
    red: "bg-red-100 text-red-600 border-red-200",
    green: "bg-green-50 text-green-700 border-green-200",
    amber: "bg-amber-50 text-amber-700 border-amber-200",
  };
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${tones[tone]}`}
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
      className="mb-3 w-64 rounded-md border border-neutral-300 bg-white px-3 py-1.5 text-sm text-neutral-800 outline-none placeholder:text-neutral-400 focus:border-neutral-500"
    />
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-neutral-300 bg-white px-6 py-10 text-center">
      <p className="text-sm font-medium text-neutral-700">{title}</p>
      {hint && <p className="mt-1 text-xs text-neutral-500">{hint}</p>}
    </div>
  );
}
