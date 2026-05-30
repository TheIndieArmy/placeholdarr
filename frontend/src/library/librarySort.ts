export function titleSortKey(title: string | null | undefined): string {
  const raw = String(title || "").trim().toLowerCase();
  return raw
    .replace(/^[^a-z0-9]+/i, "")
    .replace(/^(the|an|a)\s+/i, "")
    .replace(/^[^a-z0-9]+/i, "");
}

export function titleSortLetter(title: string | null | undefined): string {
  const key = titleSortKey(title);
  const first = key.charAt(0).toUpperCase();
  return /[A-Z]/.test(first) ? first : "#";
}
