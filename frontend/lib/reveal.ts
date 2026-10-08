/** Where the browser keeps the reveal token — proof that this viewer entered
 * the password, which the API needs before it sends real applicant names
 * instead of CAND0001-style aliases (src/api/privacy.py).
 *
 * sessionStorage, not localStorage: it survives a reload but is gone when the
 * tab closes, so real names don't stay unlocked on a machine someone else
 * picks up later.
 */

export const REVEAL_HEADER = "X-Reveal-Token";

// Masking is per-workspace (src/api/privacy.py), so the token is stored per
// workspace too: unlocking one masked workspace must not reveal another.
const STORAGE_PREFIX = "meridian.reveal.";

type StoredReveal = { token: string; expiresAt: number };

function read(workspaceId: string): StoredReveal | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_PREFIX + workspaceId);
    if (!raw) return null;
    const stored = JSON.parse(raw) as StoredReveal;
    if (!stored.token || stored.expiresAt * 1000 <= Date.now()) {
      window.sessionStorage.removeItem(STORAGE_PREFIX + workspaceId);
      return null;
    }
    return stored;
  } catch {
    // Storage blocked (private mode) or a mangled value — treat as unverified.
    return null;
  }
}

export function getRevealToken(workspaceId: string): string | null {
  return read(workspaceId)?.token ?? null;
}

/** Epoch seconds the stored token stops working at, or null if there is none. */
export function getRevealExpiry(workspaceId: string): number | null {
  return read(workspaceId)?.expiresAt ?? null;
}

export function storeRevealToken(
  workspaceId: string,
  token: string,
  expiresAt: number,
): void {
  try {
    window.sessionStorage.setItem(
      STORAGE_PREFIX + workspaceId,
      JSON.stringify({ token, expiresAt } satisfies StoredReveal),
    );
  } catch {
    /* storage unavailable — the reveal just won't outlive this page */
  }
}

export function clearRevealToken(workspaceId: string): void {
  try {
    window.sessionStorage.removeItem(STORAGE_PREFIX + workspaceId);
  } catch {
    /* nothing stored, nothing to clear */
  }
}

/** The header every API call carries once the viewer is verified for this
 * workspace. */
export function revealHeaders(workspaceId: string | null): Record<string, string> {
  if (!workspaceId) return {};
  const token = getRevealToken(workspaceId);
  return token ? { [REVEAL_HEADER]: token } : {};
}
