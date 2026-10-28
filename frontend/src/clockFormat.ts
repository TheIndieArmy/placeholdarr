/** Shared 12h / 24h clock formatting for schedule UI and task clocks. */

export type ClockFormat = "12h" | "24h";

export function parseClockFormat(raw: unknown): ClockFormat {
  const text = String(raw ?? "12h").trim().toLowerCase();
  if (text === "24" || text === "24h" || text === "24-hour" || text === "24hour") return "24h";
  return "12h";
}

export function parseHhmm(time: string): { h: number; m: number } {
  const parts = String(time || "03:00").split(":");
  const h = Math.max(0, Math.min(23, Number(parts[0]) || 0));
  const m = Math.max(0, Math.min(59, Number(parts[1]) || 0));
  return { h, m };
}

export function formatHhmm(h: number, m: number): string {
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** Format an HH:MM wall-clock string for display. */
export function formatHhmmDisplay(time: string, format: ClockFormat = "12h"): string {
  const { h, m } = parseHhmm(time);
  if (format === "24h") return formatHhmm(h, m);
  const suffix = h < 12 ? "AM" : "PM";
  let h12 = h % 12;
  if (h12 === 0) h12 = 12;
  return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
}

/** Format an absolute ISO timestamp in a given IANA zone using the clock format. */
export function formatDateTimeInZone(
  iso: string | null | undefined,
  timeZone: string | null | undefined,
  format: ClockFormat = "12h",
): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const zone = String(timeZone || "").trim() || undefined;
  try {
    return d.toLocaleString(undefined, {
      timeZone: zone,
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: format !== "24h",
    });
  } catch {
    return d.toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: format !== "24h",
    });
  }
}
