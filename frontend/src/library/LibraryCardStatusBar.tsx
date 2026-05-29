import type { LibraryItem } from "../types/api";

export type LibraryStatusCounts = {
  files: number;
  placeholders: number;
  missing: number;
  total: number;
};

const COLORS = {
  files: { light: "#64748b", dark: "#94a3b8" },
  placeholders: { light: "#0f766e", dark: "#14b8a6" },
  missing: { light: "#dc2626", dark: "#ef4444" },
  empty: { light: "#e2e8f0", dark: "#334155" },
} as const;

export function libraryStatusCounts(item: LibraryItem): LibraryStatusCounts {
  if (item.type === "series") {
    const s = item.stats;
    const files = Math.max(0, Number(s.episode_files ?? 0));
    const placeholders = Math.max(0, Number(s.episode_placeholders ?? 0));
    const missing = Math.max(0, Number(s.episode_missing ?? 0));
    const total = Math.max(Number(s.episode_total ?? 0), files + placeholders + missing);
    return { files, placeholders, missing, total };
  }
  const files = item.has_file ? 1 : 0;
  const placeholders = item.has_placeholder ? 1 : 0;
  const missing = item.has_missing ? 1 : 0;
  return { files, placeholders, missing, total: 1 };
}

function movieSolidKind(counts: LibraryStatusCounts): keyof typeof COLORS {
  if (counts.missing > 0) return "missing";
  if (counts.placeholders > 0) return "placeholders";
  if (counts.files > 0) return "files";
  return "empty";
}

function tooltipLines(counts: LibraryStatusCounts, isSeries: boolean): string {
  const lines = [
    `Real files: ${counts.files}`,
    `Placeholders: ${counts.placeholders}`,
    `Missing: ${counts.missing}`,
  ];
  if (isSeries && counts.total > 0) {
    lines.push(`Episodes tracked: ${counts.total}`);
  }
  return lines.join("\n");
}

export function LibraryCardStatusBar(props: {
  item: LibraryItem;
  isLight: boolean;
  className?: string;
  /** Sit flush against poster or card edge (no rounding gap). */
  flush?: boolean;
}) {
  const counts = libraryStatusCounts(props.item);
  const isSeries = props.item.type === "series";
  const tip = tooltipLines(counts, isSeries);
  const trackBg = props.isLight ? COLORS.empty.light : COLORS.empty.dark;
  const barRound = props.flush ? "rounded-none" : "rounded-sm";
  const wrapClass = `w-full shrink-0 block leading-none ${props.className ?? ""}`;

  if (!isSeries) {
    const kind = movieSolidKind(counts);
    const fill = kind === "empty" ? trackBg : props.isLight ? COLORS[kind].light : COLORS[kind].dark;
    return (
      <div className={wrapClass} title={tip} aria-label={tip.replace(/\n/g, ", ")}>
        <div className={`h-1 w-full overflow-hidden ${barRound}`} style={{ backgroundColor: trackBg }}>
          <div className={`h-full w-full ${barRound}`} style={{ backgroundColor: fill }} />
        </div>
      </div>
    );
  }

  const segments = [
    { key: "files" as const, n: counts.files },
    { key: "placeholders" as const, n: counts.placeholders },
    { key: "missing" as const, n: counts.missing },
  ].filter((s) => s.n > 0);

  const denom = counts.total > 0 ? counts.total : segments.reduce((a, s) => a + s.n, 0);

  return (
    <div className={wrapClass} title={tip} aria-label={tip.replace(/\n/g, ", ")}>
      <div className={`flex h-1 w-full overflow-hidden ${barRound}`} style={{ backgroundColor: trackBg }}>
        {denom <= 0 ? (
          <div className="h-full w-full" style={{ backgroundColor: trackBg }} />
        ) : (
          segments.map((seg) => (
            <div
              key={seg.key}
              className="h-full min-w-[2px] transition-[flex-grow] duration-150"
              style={{
                flexGrow: seg.n,
                flexBasis: 0,
                backgroundColor: props.isLight ? COLORS[seg.key].light : COLORS[seg.key].dark,
              }}
            />
          ))
        )}
      </div>
    </div>
  );
}
