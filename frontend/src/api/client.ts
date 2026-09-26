// HTTP client for the OwnAI API.
// - Access token kept in memory; refresh token is an httpOnly cookie (never readable by JS).
// - Every failure is normalised into ApiError {code, message, hint}; malformed or empty
//   responses raise FRONTEND_RESPONSE_ERROR instead of silently rendering nothing.

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public hint?: string,
    public requestId?: string,
    public data?: unknown,
  ) {
    super(message);
  }
}

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
const listeners = new Set<(authenticated: boolean) => void>();

export function setAccessToken(token: string | null) {
  accessToken = token;
  listeners.forEach((l) => l(token !== null));
}

export function getAccessToken() {
  return accessToken;
}

export function onAuthChange(listener: (authenticated: boolean) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

async function parseError(response: Response): Promise<ApiError> {
  let body: any = null;
  try {
    body = await response.json();
  } catch {
    /* non-JSON error body */
  }
  const err = body?.error;
  if (err?.code) return new ApiError(err.code, err.message, response.status, err.hint, err.request_id, err.data);
  if (response.status === 502 || response.status === 504)
    return new ApiError("BACKEND_UNREACHABLE", "The OwnAI server is not reachable. Is the backend running?", response.status);
  return new ApiError("HTTP_" + response.status, `Request failed (HTTP ${response.status}).`, response.status);
}

export async function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const response = await fetch(`${API_BASE}/auth/refresh`, {
          method: "POST",
          credentials: "include",
          headers: { "X-Requested-With": "ownai" },
        });
        if (!response.ok) {
          setAccessToken(null);
          return false;
        }
        const data = await response.json();
        setAccessToken(data.access_token);
        return true;
      } catch {
        setAccessToken(null);
        return false;
      } finally {
        setTimeout(() => (refreshing = null), 0);
      }
    })();
  }
  return refreshing;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  form?: FormData;
  query?: Record<string, string | number | boolean | undefined | null>;
  allowEmpty?: boolean;
}

export async function api<T = any>(path: string, options: RequestOptions = {}, retry = true): Promise<T> {
  const url = new URL(API_BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  const headers: Record<string, string> = { "X-Requested-With": "ownai" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let body: BodyInit | undefined;
  if (options.form) body = options.form;
  else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  let response: Response;
  try {
    response = await fetch(url.toString().replace(window.location.origin, ""), {
      method: options.method ?? (body ? "POST" : "GET"),
      headers,
      body,
      credentials: "include",
    });
  } catch {
    throw new ApiError("BACKEND_UNREACHABLE", "Cannot reach the OwnAI server. Check that the backend is running.", 0);
  }
  if (response.status === 401 && retry && !path.startsWith("/auth/")) {
    if (await refreshSession()) return api<T>(path, options, false);
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) {
    if (options.allowEmpty) return undefined as T;
    throw new ApiError("FRONTEND_RESPONSE_ERROR", "The server returned an empty response.", response.status);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError("FRONTEND_RESPONSE_ERROR", "The server returned a response that is not valid JSON.", response.status);
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.hint ? `${error.message} ${error.hint}` : error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}
