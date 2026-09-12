// One GET against the daemon's versioned API (DESIGN/v0-5-0.md §2): the
// session's token as a bearer, JSON back, `{"error": ...}` bodies raised as
// ApiError(status). Nothing here ever issues another verb: the dashboard is
// read-only by construction.

import { getToken } from "../auth/token";

export const API_BASE = "/api/v1";

export type ParamValue =
  string | number | boolean | string[] | null | undefined;
export type Params = Record<string, ParamValue>;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The query string the way `ControlClient` builds it: repeated keys for
 * lists, `1` for true, nothing for false/null/undefined. */
export function buildQuery(params?: Params): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, item);
    } else {
      search.append(key, value === true ? "1" : String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// A 401 means the stored token is not this daemon's: the layout says so and
// leaves the token alone (a `tfs ui` link replaces it).
let tokenRejected = false;
const listeners = new Set<() => void>();

export function isTokenRejected(): boolean {
  return tokenRejected;
}

export function subscribeTokenRejected(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function rejectToken(): void {
  if (tokenRejected) return;
  tokenRejected = true;
  for (const listener of listeners) listener();
}

/** For tests: forget a rejection. */
export function resetTokenRejected(): void {
  tokenRejected = false;
}

export async function api<T>(
  path: string,
  params?: Params,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(params)}`, {
      headers,
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    const reason = error instanceof Error ? error.message : String(error);
    throw new ApiError(0, `the daemon did not answer (${reason})`);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "error" in body) {
        const text = (body as { error: unknown }).error;
        if (typeof text === "string") message = text;
      }
    } catch {
      // not JSON: the status line is all there is
    }
    if (response.status === 401) rejectToken();
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}
