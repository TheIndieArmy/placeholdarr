const SETUP_COMPLETE_SESSION_KEY = "placeholdarr:setup-complete";

export function markSetupCompleteInSession(): void {
  try {
    sessionStorage.setItem(SETUP_COMPLETE_SESSION_KEY, "1");
  } catch {
    /* private mode / quota */
  }
}

export function isSetupCompleteInSession(): boolean {
  try {
    return sessionStorage.getItem(SETUP_COMPLETE_SESSION_KEY) === "1";
  } catch {
    return false;
  }
}

export function clearSetupCompleteInSession(): void {
  try {
    sessionStorage.removeItem(SETUP_COMPLETE_SESSION_KEY);
  } catch {
    /* ignore */
  }
}
