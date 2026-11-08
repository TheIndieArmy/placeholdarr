/** Shared date-token format ids for Custom/Text layouts and Status Updates. */

export type DateFormatOption = { id: string; label: string; example: string };

export const DATE_FORMAT_OPTIONS: DateFormatOption[] = [
  { id: "iso", label: "ISO", example: "2026-09-30" },
  { id: "short", label: "Short", example: "Sep 30" },
  { id: "medium", label: "Medium", example: "Sep 30, 2026" },
  { id: "long", label: "Long", example: "September 30, 2026" },
  { id: "mdy", label: "Numeric", example: "MM/DD/YYYY" },
  { id: "dmy", label: "Numeric", example: "DD/MM/YYYY" },
  { id: "ymd", label: "Numeric", example: "YYYY/MM/DD" },
];

const DATE_FORMAT_ALIASES: Record<string, string> = { numeric: "mdy" };

/** Status Updates + poster Text templates (ISO values that accept ``:format``). */
export const STATUS_AND_LAYOUT_DATE_TOKENS = new Set([
  "TheatricalDate",
  "DigitalDate",
  "PhysicalDate",
  "AirDate",
  "SeriesPremiereDate",
  "SeasonPremiereDate",
  "ReleaseDate",
]);

/** Layout stamp: ``Label - date`` (preferred release / next air). Format rewrites the date half. */
export const LAYOUT_STAMPED_DATE_TOKENS = new Set(["ComingSoonOrNewEp"]);

export function normalizeDateFormat(fmt: string | null | undefined): string {
  let key = String(fmt || "iso").trim().toLowerCase() || "iso";
  key = DATE_FORMAT_ALIASES[key] || key;
  if (DATE_FORMAT_OPTIONS.some((opt) => opt.id === key)) return key;
  return "iso";
}

function parseDateValue(raw: string): Date | null {
  const text = String(raw || "").trim();
  if (!text) return null;
  const head = text.slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(head)) {
    const [y, m, d] = head.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    if (dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d) return dt;
  }
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDateToken(raw: string, fmt: string | null | undefined): string {
  const parsed = parseDateValue(raw);
  if (!parsed) return "";
  const key = normalizeDateFormat(fmt);
  const monthsShort = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const monthsLong = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
  ];
  const month = parsed.getMonth();
  const day = parsed.getDate();
  const year = parsed.getFullYear();
  if (key === "short") return `${monthsShort[month]} ${day}`;
  if (key === "medium") return `${monthsShort[month]} ${day}, ${year}`;
  if (key === "long") return `${monthsLong[month]} ${day}, ${year}`;
  if (key === "mdy") return `${month + 1}/${day}/${year}`;
  if (key === "dmy") return `${day}/${month + 1}/${year}`;
  if (key === "ymd") {
    return `${year}/${String(month + 1).padStart(2, "0")}/${String(day).padStart(2, "0")}`;
  }
  return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}
