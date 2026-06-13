// localStorage wrapper for the admin token. Plaintext — same trust boundary
// as keeping the token in your clipboard. Read-only observability dashboard,
// no participant keys are stored.

const ADMIN_TOKEN_KEY = "goa.admin_token";

export function getAdminToken(): string | null {
  return localStorage.getItem(ADMIN_TOKEN_KEY);
}

export function setAdminToken(token: string): void {
  localStorage.setItem(ADMIN_TOKEN_KEY, token);
}

export function clearAdminToken(): void {
  localStorage.removeItem(ADMIN_TOKEN_KEY);
}
