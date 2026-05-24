// Bare fetch wrapper. Same-origin in prod; in dev Vite proxies /admin to
// the hub on :8000 (see vite.config.ts).

import { clearAdminToken } from "../lib/storage";

export class GoaError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  authToken?: string | null;
  query?: Record<string, string | number | boolean | undefined> | URLSearchParams;
}

function buildUrl(
  path: string,
  query?: RequestOptions["query"],
): string {
  if (!query) return path;
  let qs: URLSearchParams;
  if (query instanceof URLSearchParams) {
    qs = query;
  } else {
    qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined) continue;
      qs.append(k, String(v));
    }
  }
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

export async function request<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (opts.authToken) {
    headers["Authorization"] = `Bearer ${opts.authToken}`;
  }
  const url = buildUrl(path, opts.query);
  const response = await fetch(url, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!response.ok) {
    let code = "error";
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.error?.code) {
        code = body.error.code;
        message = body.error.message ?? message;
      }
    } catch {
      // body wasn't JSON; keep status-line message
    }
    if (response.status === 401) {
      // Stored token is no longer valid (rotated, revoked, or wrong from the
      // start). Drop it so the gate re-renders; App.tsx watches the query
      // cache for the same 401 and flips back to the gate.
      clearAdminToken();
    }
    throw new GoaError(code, message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
