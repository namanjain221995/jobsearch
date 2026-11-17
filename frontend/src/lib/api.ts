/**
 * The one way the browser talks to the backend.
 *
 * - Only relative /api/... URLs: Next.js forwards them to the API server, so the
 *   HttpOnly session cookie is same-origin and no token is ever held in script.
 * - Every state-changing request echoes the readable `jf_csrf` cookie in the
 *   X-CSRF-Token header.
 * - Errors arrive as {"error", "detail", "fields"?} and are raised as ApiError.
 * - A 401 sends the user to /login?next=<current path>.
 */

export const PUBLIC_PATHS = ["/login", "/forgot-password", "/reset-password", "/invite/accept"] as const;

const CSRF_COOKIE = "jf_csrf";
const CSRF_HEADER = "X-CSRF-Token";
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly fields: Record<string, string>;

  constructor(status: number, message: string, detail = "", fields: Record<string, string> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.fields = fields;
  }

  /** The signed-in user is not allowed to do or see this. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** It was this person's and has been taken away on purpose (410: a job a manager took back). */
  get isGone(): boolean {
    return this.status === 410;
  }

  /** The request never reached the API (offline, or the API server is down). */
  get isNetwork(): boolean {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export interface ApiOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: QueryParams;
  /** Sent as JSON. */
  body?: unknown;
  /** Sent as multipart/form-data (file uploads). */
  formData?: FormData;
  signal?: AbortSignal;
  /** Set to false on the sign-in screens, where a 401 is an answer and not a lost session. */
  redirectOn401?: boolean;
}

function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  for (const part of document.cookie.split("; ")) {
    const index = part.indexOf("=");
    if (index > 0 && part.slice(0, index) === name) {
      try {
        return decodeURIComponent(part.slice(index + 1));
      } catch {
        return part.slice(index + 1);
      }
    }
  }
  return "";
}

export function buildUrl(path: string, query?: QueryParams): string {
  if (!path.startsWith("/api/")) {
    throw new Error(`apiFetch only calls relative /api/ paths (got "${path}")`);
  }
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

/** Only same-site paths are accepted as a place to return to after signing in. */
export function safeNextPath(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return null;
  if (value.startsWith("/api/") || value.startsWith("/_next/")) return null;
  if (PUBLIC_PATHS.some((p) => value === p || value.startsWith(`${p}?`) || value.startsWith(`${p}/`))) return null;
  return value;
}

let redirecting = false;
let stopped = false;

/**
 * Called once the session has been ended on purpose (sign-out). From then on no
 * request leaves this page, so nothing that was polling can ask the API again
 * with a session that no longer exists while the browser moves to the sign-in screen.
 */
export function stopApi(): void {
  stopped = true;
}

function redirectToLogin(): void {
  if (typeof window === "undefined" || redirecting) return;
  const { pathname, search } = window.location;
  if (PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return;
  redirecting = true;
  const next = safeNextPath(pathname + search);
  window.location.assign(next ? `/login?next=${encodeURIComponent(next)}` : "/login");
}

function fallbackMessage(status: number): string {
  if (status === 401) return "Your session has ended. Sign in again.";
  if (status === 403) return "You do not have access to this resource";
  if (status === 404) return "Not found";
  if (status === 410) return "This is no longer available.";
  if (status === 413) return "The file is too large.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status === 502 || status === 503 || status === 504) {
    return "The service is not reachable right now. Try again in a moment.";
  }
  return "Something went wrong on the server. Try again.";
}

async function toApiError(response: Response): Promise<ApiError> {
  let message = "";
  let detail = "";
  let fields: Record<string, string> = {};
  try {
    const type = response.headers.get("content-type") ?? "";
    if (type.includes("application/json")) {
      const data: unknown = await response.json();
      if (data && typeof data === "object") {
        const record = data as Record<string, unknown>;
        if (typeof record.error === "string") message = record.error;
        if (typeof record.detail === "string") detail = record.detail;
        if (record.fields && typeof record.fields === "object") {
          fields = Object.fromEntries(
            Object.entries(record.fields as Record<string, unknown>).map(([key, value]) => [key, String(value)]),
          );
        }
      }
    }
  } catch {
    // The body was not the JSON error shape; the status code still tells the story.
  }
  return new ApiError(response.status, message || fallbackMessage(response.status), detail, fields);
}

export async function apiFetch<T>(path: string, options: ApiOptions = {}): Promise<T> {
  // The page is on its way to the sign-in screen: settle nothing and send nothing.
  if (stopped || redirecting) return new Promise<T>(() => undefined);
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (options.formData) {
    body = options.formData;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (UNSAFE_METHODS.has(method)) {
    const token = readCookie(CSRF_COOKIE);
    if (token) headers[CSRF_HEADER] = token;
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      body,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "Cannot reach the server. Check your connection and try again.");
  }

  if (response.status === 401 && options.redirectOn401 !== false) {
    redirectToLogin();
    if (redirecting) {
      // The page is leaving for the sign-in screen: settle nothing, so no error flashes on the way out.
      return new Promise<T>(() => undefined);
    }
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as T;
}

export const api = {
  get: <T>(path: string, query?: QueryParams, signal?: AbortSignal) => apiFetch<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, options?: Pick<ApiOptions, "redirectOn401" | "query">) =>
    apiFetch<T>(path, { method: "POST", body, ...options }),
  patch: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: "DELETE" }),
  upload: <T>(path: string, formData: FormData) => apiFetch<T>(path, { method: "POST", formData }),
};

/** A sentence a person can act on, for toasts and inline messages. */
export function errorMessage(error: unknown, fallback = "Something went wrong. Try again."): string {
  if (error instanceof ApiError) {
    return error.detail && error.message === "Invalid input" ? `${error.message}: ${error.detail}` : error.message;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}
