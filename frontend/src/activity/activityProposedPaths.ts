export const ACTIVITY_PROPOSED_PREFIX = "/activity/proposed";
export const ACTIVITY_PROPOSED_PLACEHOLDERS_PATH = `${ACTIVITY_PROPOSED_PREFIX}/placeholders`;
export const ACTIVITY_PROPOSED_TASKS_PATH = `${ACTIVITY_PROPOSED_PREFIX}/tasks`;

export function isActivityProposedPath(pathname: string): boolean {
  const p = pathname.replace(/\/$/, "") || "/";
  return p === ACTIVITY_PROPOSED_PREFIX || p.startsWith(`${ACTIVITY_PROPOSED_PREFIX}/`);
}
