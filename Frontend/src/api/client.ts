// The calls against the daemon's versioned API (DESIGN/v0-5-0.md §2, §12):
// the session's token as a bearer on every one, `{"error": ...}` bodies
// raised as ApiError(status). Four shapes and no more: `api` (GET JSON),
// `apiPost` (POST JSON), `apiUpload` (POST bytes), `apiBlob` (GET bytes).
// The dashboard reads with the first; the upload box, the tag editor and
// the download button are the only callers of the other three.

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

interface RequestOptions {
  method?: "GET" | "POST";
  body?: BodyInit;
  contentType?: string;
  accept?: string;
  signal?: AbortSignal;
}

/** One request with the token; a non-2xx answer becomes an ApiError with
 * the daemon's message. Returns the raw Response for the caller to read. */
async function request(
  path: string,
  params: Params | undefined,
  options: RequestOptions,
): Promise<Response> {
  const headers: Record<string, string> = {
    Accept: options.accept ?? "application/json",
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (options.contentType) headers["Content-Type"] = options.contentType;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(params)}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body,
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
  return response;
}

/** GET, JSON back. */
export async function api<T>(
  path: string,
  params?: Params,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const response = await request(path, params, { signal: options.signal });
  return (await response.json()) as T;
}

/** POST with an optional JSON body, JSON back (the writes of §11.7/§12). */
export async function apiPost<T>(
  path: string,
  params?: Params,
  body?: unknown,
): Promise<T> {
  const response = await request(path, params, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    contentType: body === undefined ? undefined : "application/json",
  });
  return (await response.json()) as T;
}

/** POST with the bytes of a file as the body (`/files/upload`). */
export async function apiUpload<T>(
  path: string,
  params: Params,
  data: Blob,
): Promise<T> {
  const response = await request(path, params, {
    method: "POST",
    body: data,
    contentType: data.type || "application/octet-stream",
  });
  return (await response.json()) as T;
}

/** The file name a `Content-Disposition` header carries, if any. */
export function dispositionName(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header)?.[1];
  if (extended) {
    try {
      return decodeURIComponent(extended);
    } catch {
      // fall through to the plain name
    }
  }
  return /filename="?([^";]+)"?/i.exec(header)?.[1] ?? null;
}

/** GET answered as bytes (`/file/content`): the blob and the name the
 * daemon gave it, for the browser to save. The token stays in the header —
 * never in a URL the browser could bookmark (DESIGN/v0-5-0.md §12.2). */
export async function apiBlob(
  path: string,
  params?: Params,
): Promise<{ blob: Blob; filename: string | null }> {
  const response = await request(path, params, { accept: "*/*" });
  return {
    blob: await response.blob(),
    filename: dispositionName(response.headers.get("Content-Disposition")),
  };
}
