/** Where the browser keeps the reveal token — proof that this viewer entered
 * the password, which the API needs before it sends real applicant names
 * instead of CAND0001-style aliases (src/api/privacy.py).
 *
 * sessionStorage, not localStorage: it survives a reload but is gone when the
 * tab closes, so real names don't stay unlocked on a machine someone else
 * picks up later.
 */

export const REVEAL_HEADER = "X-Reveal-Token";

const STORAGE_KEY = "meridian.reveal";

type StoredReveal = { token: string; expiresAt: number };

function read(): StoredReveal | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as StoredReveal;
    if (!stored.token || stored.expiresAt * 1000 <= Date.now()) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return stored;
  } catch {
    // Storage blocked (private mode) or a mangled value — treat as unverified.
    return null;
  }
}

export function getRevealToken(): string | null {
  return read()?.token ?? null;
}

/** Epoch seconds the stored token stops working at, or null if there is none. */
export function getRevealExpiry(): number | null {
  return read()?.expiresAt ?? null;
}

export function storeRevealToken(token: string, expiresAt: number): void {
  try {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ token, expiresAt } satisfies StoredReveal),
    );
  } catch {
    /* storage unavailable — the reveal just won't outlive this page */
  }
}

export function clearRevealToken(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* nothing stored, nothing to clear */
  }
}

/** The header every API call carries once the viewer is verified. */
export function revealHeaders(): Record<string, string> {
  const token = getRevealToken();
  return token ? { [REVEAL_HEADER]: token } : {};
}
