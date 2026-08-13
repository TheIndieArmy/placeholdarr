function humanizeFetchFailure(err: unknown): Error {
  if (err instanceof TypeError) {
    return new Error(
      "Cannot reach the Placeholdarr API (network error). Trying to reconnect…",
    );
  }
  if (err instanceof Error && /failed to fetch/i.test(err.message)) {
    return new Error(
      "Cannot reach the Placeholdarr API. Confirm the backend is running and the page is served from the same origin as /api (or configure your reverse proxy).",
    );
  }
  return err instanceof Error ? err : new Error(String(err));
}

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token && String(token).trim() ? String(token).trim() : null;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

function isMutating(method: string | undefined): boolean {
  const m = (method || "GET").toUpperCase();
  return m === "POST" || m === "PUT" || m === "PATCH" || m === "DELETE";
}

function mergeHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers || undefined);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (isMutating(init?.method) && csrfToken && !headers.has("X-CSRF-Token")) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  return headers;
}

async function parseErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = (await response.json()) as { message?: string; detail?: string };
    return payload.message || payload.detail || fallback;
  } catch {
    return fallback;
  }
}

export class ApiUnauthorizedError extends Error {
  constructor(message = "authentication required") {
    super(message);
    this.name = "ApiUnauthorizedError";
  }
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...init,
      headers: mergeHeaders(init),
    });
  } catch (err) {
    throw humanizeFetchFailure(err);
  }

  if (response.status === 401) {
    unauthorizedHandler?.();
    const message = await parseErrorMessage(response, "authentication required");
    throw new ApiUnauthorizedError(message);
  }

  if (!response.ok) {
    const message = await parseErrorMessage(response, `Request failed: ${response.status}`);
    throw new Error(message);
  }

  return (await response.json()) as T;
}

export async function postJson<T>(path: string, body?: unknown): Promise<T> {
  return fetchJson<T>(path, {
    method: "POST",
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
