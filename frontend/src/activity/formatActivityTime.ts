export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "--";
  const d = new Date(iso);
  const now = new Date();
  const s = Math.floor((now.getTime() - d.getTime()) / 1000);
  if (!Number.isFinite(s)) return "--";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function timeUntil(iso: string | null | undefined): string {
  if (!iso) return "--";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "--";
  const diffMs = t - Date.now();
  if (diffMs <= 0) return "now";
  const mins = Math.floor(diffMs / 60000);
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `in ${hours} hr`;
  const days = Math.floor(hours / 24);
  return `in ${days} day${days === 1 ? "" : "s"}`;
}

export function formatTaskDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "--";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function formatTaskTrigger(trigger: string | null | undefined): string {
  const normalized = String(trigger || "").trim().toLowerCase();
  if (normalized === "settings_change") return "Settings Change";
  if (!normalized) return "--";
  return normalized.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

export function dayGroupLabel(iso: string | null | undefined): string {
  if (!iso) return "Unknown date";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Unknown date";
  const now = new Date();
  const startOf = (dt: Date) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
  const diffDays = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export function remainderMissing(
  total: number | undefined,
  downloaded: number | undefined,
  placeholders: number | undefined,
  future: number | undefined,
): number {
  return Math.max(0, (total ?? 0) - (downloaded ?? 0) - (placeholders ?? 0) - (future ?? 0));
}
