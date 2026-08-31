import type { CSSProperties, ReactNode } from "react";
import type { Brand } from "../../brandTypes";
import type { ThemeMode } from "../../brandTypes";
import type { ArrInstanceOpenLink } from "../../types/api";
import type { SeriesSeasonDetail } from "../../types/api";
import placeholdarrLogoYellow from "../../assets/Placeholdarr_yellow.svg";
import radarrIcon from "../../assets/services/radarr.svg";
import sonarrIcon from "../../assets/services/sonarr.svg";

const MOVIE_FILE_STATE_RADARR_LOGO_WELL: CSSProperties = {
  backgroundColor: "#1e2430",
  border: "2px solid rgba(250, 204, 21, 0.78)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
};

const MOVIE_FILE_STATE_PLACEHOLDARR_LOGO_WELL: CSSProperties = {
  backgroundColor: "#1e2430",
  border: "2px solid rgba(251, 191, 36, 0.78)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
};

const SERIES_FILE_STATE_SONARR_LOGO_WELL: CSSProperties = {
  backgroundColor: "#1e2430",
  border: "2px solid rgba(56, 189, 248, 0.8)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
};

/**
 * Single furthest-along movie ARR status:
 * Not in library → Unmonitored → Monitored → Downloaded
 * Downloaded wins over monitored once a file exists.
 */
function movieArrInstanceStatus(row: {
  present: boolean;
  has_file_known: boolean;
  has_file: boolean;
  monitored?: boolean;
}): string {
  if (!row.present) return "Not in library";
  if (row.has_file_known && row.has_file) return "Downloaded";
  if (row.monitored === false) return "Unmonitored";
  if (row.monitored === true) return "Monitored";
  // Present but we don't know file/monitored yet
  return "In library";
}

/**
 * Single furthest-along series ARR status (option B):
 * Not in library → Unmonitored → Monitored → files/total (partial) → Downloaded
 */
function seriesArrInstanceStatus(row: {
  present: boolean;
  episode_files: number;
  episode_total: number;
  monitored?: boolean;
}): string {
  if (!row.present) return "Not in library";
  const files = Math.max(0, Math.floor(Number(row.episode_files) || 0));
  const total = Math.max(0, Math.floor(Number(row.episode_total) || 0));
  if (total > 0 && files >= total) return "Downloaded";
  if (files > 0) {
    if (total > 0) return `${files}/${total}`;
    return String(files);
  }
  if (row.monitored === false) return "Unmonitored";
  if (row.monitored === true) return "Monitored";
  return "In library";
}

export function MovieFileStateSection(props: {
  links: ArrInstanceOpenLink[] | undefined;
  arrLink?: string | null;
  hasFile: boolean;
  hasPlaceholder: boolean;
  instanceLabel?: string | null;
  monitored?: boolean | null;
  isLight: boolean;
  brandLabel: string;
  accentHex: string;
}) {
  void props.accentHex;
  const instanceLabel = String(props.instanceLabel || "Radarr").trim() || "Radarr";
  const rawMovieLinks = props.links;
  const linkRows: {
    label: string;
    url: string;
    present: boolean;
    has_file: boolean;
    has_file_known: boolean;
    has_placeholder: boolean;
    monitored?: boolean;
  }[] = Array.isArray(rawMovieLinks) && rawMovieLinks.length
    ? rawMovieLinks.map((l) => ({
        label: l.label,
        url: l.url,
        present: l.present !== false,
        has_file: l.has_file === true,
        has_file_known: typeof l.has_file === "boolean",
        has_placeholder: Boolean(l.has_placeholder),
        monitored: typeof l.monitored === "boolean" ? l.monitored : undefined,
      }))
    : rawMovieLinks == null
      ? (() => {
          const u = String(props.arrLink || "").trim();
          if (!u) return [];
          return [
            {
              label: instanceLabel,
              url: u,
              present: true,
              has_file: props.hasFile,
              has_file_known: true,
              has_placeholder: props.hasPlaceholder,
              monitored: typeof props.monitored === "boolean" ? props.monitored : undefined,
            },
          ];
        })()
      : [];

  const placeholderOnDisk = Boolean(props.hasPlaceholder) || linkRows.some((r) => r.has_placeholder);

  return (
    <div
      className={`mb-4 rounded-lg border px-3 py-3 md:px-4 md:py-3 ${
        props.isLight ? "border-[#d7e2f0] bg-white shadow-sm" : "border-[#424753]/40 bg-[#171c22]"
      }`}
    >
      <div className="flex w-full flex-wrap items-stretch justify-center gap-3">
        <div
          className="movie-file-state-dark-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
          role="group"
          aria-label="Placeholder dummy on disk"
        >
          <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl" style={MOVIE_FILE_STATE_PLACEHOLDARR_LOGO_WELL}>
            <img src={placeholdarrLogoYellow} alt="" className="h-10 w-auto max-w-[4.75rem] object-contain" aria-hidden />
          </div>
          <div className="movie-file-state-tile-title font-semibold font-headline">{props.brandLabel}</div>
          <div className="movie-file-state-tile-status text-[18px] font-bold font-headline leading-snug">
            {placeholderOnDisk ? "Placeholder" : "No placeholder"}
          </div>
        </div>
        {linkRows.map((row, idx) => {
          const status = movieArrInstanceStatus(row);
          return (
            <a
              key={`${row.url}-${idx}`}
              href={row.url}
              target="_blank"
              rel="noreferrer"
              className="movie-file-state-dark-tile movie-file-state-arr-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
            >
              <div className="movie-file-state-arr-well flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl" style={MOVIE_FILE_STATE_RADARR_LOGO_WELL}>
                <img src={radarrIcon} alt="" decoding="async" className="h-12 w-12 object-contain" aria-hidden />
              </div>
              <div className="movie-file-state-tile-title font-semibold font-headline">{row.label}</div>
              <div className="movie-file-state-tile-status text-[18px] font-bold font-headline leading-snug">{status}</div>
            </a>
          );
        })}
      </div>
    </div>
  );
}

export function SeriesFileStateSection(props: {
  seasons: SeriesSeasonDetail[];
  links: ArrInstanceOpenLink[] | undefined;
  arrLink?: string | null;
  instanceLabel?: string | null;
  monitored?: boolean | null;
  isLight: boolean;
  brandLabel: string;
}) {
  const instanceLabel = String(props.instanceLabel || "Sonarr").trim() || "Sonarr";
  const rawLinks = props.links;
  const linkRows: {
    label: string;
    url: string;
    present: boolean;
    episode_files: number;
    episode_total: number;
    monitored?: boolean;
  }[] = Array.isArray(rawLinks) && rawLinks.length
    ? rawLinks.map((l) => ({
        label: l.label,
        url: l.url,
        present: l.present !== false,
        episode_files: typeof l.episode_files === "number" ? l.episode_files : 0,
        episode_total: typeof l.episode_total === "number" ? l.episode_total : 0,
        monitored: typeof l.monitored === "boolean" ? l.monitored : undefined,
      }))
    : rawLinks == null
      ? (() => {
          const u = String(props.arrLink || "").trim();
          if (!u) return [];
          const files = (props.seasons || []).reduce((a, s) => a + Number(s.episode_files || 0), 0);
          const total = (props.seasons || []).reduce((a, s) => a + Number(s.episode_total || 0), 0);
          return [
            {
              label: instanceLabel,
              url: u,
              present: true,
              episode_files: files,
              episode_total: total,
              monitored: typeof props.monitored === "boolean" ? props.monitored : undefined,
            },
          ];
        })()
      : [];

  const aggPlaceholders = (props.seasons || []).reduce((a, s) => a + Number(s.episode_placeholders || 0), 0);
  const phTotalStr = String(Math.max(0, Math.floor(aggPlaceholders)));

  return (
    <div
      className={`mb-4 rounded-lg border px-3 py-3 md:px-4 md:py-3 ${
        props.isLight ? "border-[#d7e2f0] bg-white shadow-sm" : "border-[#424753]/40 bg-[#171c22]"
      }`}
    >
      <div className="flex w-full flex-wrap items-stretch justify-center gap-3">
        <div
          className="movie-file-state-dark-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
          role="group"
          aria-label="Episodes with placeholder files"
        >
          <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl" style={MOVIE_FILE_STATE_PLACEHOLDARR_LOGO_WELL}>
            <img src={placeholdarrLogoYellow} alt="" className="h-10 w-auto max-w-[4.75rem] object-contain" aria-hidden />
          </div>
          <div className="movie-file-state-tile-title font-semibold font-headline">{props.brandLabel}</div>
          <div className="mt-auto flex flex-col items-center gap-0.5">
            <div className="movie-file-state-tile-status text-[26px] font-black font-headline tabular-nums leading-none">{phTotalStr}</div>
            <div className="movie-file-state-tile-caption text-[12px] font-headline font-medium uppercase tracking-wider">Placeholders</div>
          </div>
        </div>
        {linkRows.map((row, idx) => {
          const status = seriesArrInstanceStatus(row);
          return (
            <a
              key={`${row.label}-${row.url}-${idx}`}
              href={row.url}
              target="_blank"
              rel="noreferrer"
              className="movie-file-state-dark-tile movie-file-state-arr-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
            >
              <div className="movie-file-state-arr-well flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl" style={SERIES_FILE_STATE_SONARR_LOGO_WELL}>
                <img src={sonarrIcon} alt="" decoding="async" className="h-12 w-12 object-contain" aria-hidden />
              </div>
              <div className="movie-file-state-tile-title font-semibold font-headline">{row.label}</div>
              <div className="movie-file-state-tile-status text-[18px] font-bold font-headline leading-snug tabular-nums">{status}</div>
            </a>
          );
        })}
      </div>
    </div>
  );
}

export function DetailLayoutShell(props: {
  brand: Brand;
  themeMode: ThemeMode;
  children: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  return (
    <div className={`min-h-screen ${isLight ? "bg-[#eef3f8]" : "bg-[#0f1419]"}`}>{props.children}</div>
  );
}
