import { fetchJson, postJson, setCsrfToken } from "./client";

export type AuthMode = "builtin" | "forward_auth" | "disabled";

export type AuthStatus = {
  mode: AuthMode;
  configured: boolean;
  authenticated: boolean;
  username?: string | null;
  csrf_token?: string | null;
  forward_auth_ready?: boolean | null;
  trusted_proxy?: boolean | null;
  auth_source?: string | null;
};

function rememberCsrf(status: AuthStatus): AuthStatus {
  if (status.csrf_token) {
    setCsrfToken(status.csrf_token);
  }
  return status;
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const status = await fetchJson<AuthStatus>("/api/auth/status");
  return rememberCsrf(status);
}

export async function setupAuth(username: string, password: string): Promise<AuthStatus> {
  const status = await postJson<AuthStatus>("/api/auth/setup", { username, password });
  return rememberCsrf(status);
}

export async function loginAuth(username: string, password: string): Promise<AuthStatus> {
  const status = await postJson<AuthStatus>("/api/auth/login", { username, password });
  return rememberCsrf(status);
}

export async function logoutAuth(): Promise<AuthStatus> {
  const status = await postJson<AuthStatus>("/api/auth/logout");
  return rememberCsrf(status);
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<{ ok: boolean; message?: string }> {
  return postJson<{ ok: boolean; message?: string }>("/api/auth/change-password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}
