import type { DetailRatingDisplay } from "../../types/api";

export type { DetailRatingDisplay };

export type DetailActorDisplay = {
  name: string;
  role?: string | null;
};

export function alphaColor(hex: string, alpha: number): string {
  const raw = String(hex || "").replace("#", "").trim();
  if (raw.length !== 6) return `rgba(0,0,0,${alpha})`;
  const r = parseInt(raw.slice(0, 2), 16);
  const g = parseInt(raw.slice(2, 4), 16);
  const b = parseInt(raw.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

export function formatRuntimeMinutes(minutes: number | null | undefined): string | null {
  const m = Number(minutes);
  if (!Number.isFinite(m) || m <= 0) return null;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  if (h <= 0) return `${m} min`;
  if (rem === 0) return `${h} hr`;
  return `${h} hr ${rem} min`;
}

export function formatFileSize(bytes: number | null | undefined): string | null {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return null;
  const gb = n / (1024 ** 3);
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  const mb = n / (1024 ** 2);
  return `${mb.toFixed(1)} MB`;
}

export function formatDeterminationLabel(raw: string | null | undefined): string {
  const v = String(raw || "").trim().toLowerCase();
  if (!v) return "—";
  return v.replace(/_/g, " ");
}

export function formatMonitoredLabel(monitored: boolean | null | undefined): string {
  return monitored ? "Monitored" : "Unmonitored";
}

/** Human-readable placeholder display status (REQUEST, SEARCHING, …). */
export function formatDisplayStatusLabel(raw: string | null | undefined): string {
  const v = String(raw || "").trim();
  if (!v) return "—";
  return v
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatSonarrStatusLabel(raw: string | null | undefined): string {
  const v = String(raw || "").trim().toLowerCase();
  if (!v) return "—";
  if (v === "continuing") return "Continuing";
  if (v === "ended") return "Ended";
  if (v === "upcoming") return "Upcoming";
  return v.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function detailMutedChipClass(isLight: boolean): string {
  return `px-2 py-0.5 rounded border text-[11px] font-headline uppercase tracking-wider ${
    isLight ? "border-slate-200 bg-slate-50 text-slate-700" : "border-[#424753]/50 bg-[#1e2430] text-slate-300"
  }`;
}

export function detailStatusChipClass(isLight: boolean, kind: "file" | "placeholder" | "missing"): string {
  const base = detailMutedChipClass(isLight);
  if (kind === "file") {
    return `${base} ${isLight ? "text-emerald-800" : "text-emerald-200"}`;
  }
  if (kind === "placeholder") {
    return `${base} ${isLight ? "text-sky-800" : "text-sky-200"}`;
  }
  return `${base} ${isLight ? "text-red-800" : "text-red-200"}`;
}

export function formatRatingLabel(source: string | null | undefined): string {
  const key = String(source || "").trim().toLowerCase();
  if (!key) return "Rating";
  if (key === "rottentomatoes" || key === "rotten_tomatoes" || key === "rotten tomatoes") return "RT";
  if (key === "metacritic") return "Metacritic";
  if (key === "themoviedb" || key === "tmdb") return "TMDB";
  if (key === "imdb") return "IMDB";
  if (key === "trakt") return "Trakt";
  return key.toUpperCase();
}

/** Round 0–10 style scores to one decimal; leave percent scores (RT/Metacritic) as-is. */
export function formatRatingValue(source: string | null | undefined, value: string | null | undefined): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const key = String(source || "").trim().toLowerCase();
  if (key === "tmdb" || key === "themoviedb" || key === "trakt" || key === "imdb") {
    const n = Number(raw);
    if (Number.isFinite(n)) return (Math.round(n * 10) / 10).toFixed(1);
  }
  return raw;
}

export function ratingPageUrl(
  source: string,
  ids: { imdbid?: string | null; tmdbid?: number | null; tmdbTvId?: number | null },
): string | null {
  const key = String(source || "").trim().toLowerCase();
  if (key === "imdb") return imdbUrl(ids.imdbid);
  if (key === "tmdb") {
    const tv = Number(ids.tmdbTvId);
    if (Number.isFinite(tv) && tv > 0) return `https://www.themoviedb.org/tv/${tv}`;
    return tmdbMovieUrl(ids.tmdbid);
  }
  return null;
}

export function youtubeTrailerUrl(raw: string | null | undefined): string | null {
  const value = String(raw || "").trim();
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return value;
  // Radarr stores youTubeTrailerId as a bare video id.
  if (/^[\w-]{6,20}$/.test(value)) return `https://www.youtube.com/watch?v=${value}`;
  return null;
}

export function imdbUrl(imdbid: string | null | undefined): string | null {
  const id = String(imdbid || "").trim();
  if (!id) return null;
  return `https://www.imdb.com/title/${id}/`;
}

export function tmdbMovieUrl(tmdbid: number | null | undefined): string | null {
  const id = Number(tmdbid);
  if (!Number.isFinite(id) || id <= 0) return null;
  return `https://www.themoviedb.org/movie/${id}`;
}

export function tmdbTvUrl(tmdbId: number | null | undefined, tvdbid?: number | null): string | null {
  const tmdb = Number(tmdbId);
  if (Number.isFinite(tmdb) && tmdb > 0) return `https://www.themoviedb.org/tv/${tmdb}`;
  const id = Number(tvdbid);
  if (!Number.isFinite(id) || id <= 0) return null;
  return `https://www.thetvdb.com/?tab=series&id=${id}`;
}

export function primaryRating(ratings: DetailRatingDisplay[] | null | undefined): DetailRatingDisplay | null {
  if (!ratings?.length) return null;
  return ratings.find((r) => r.is_default) ?? ratings.find((r) => r.source === "tmdb") ?? ratings[0];
}
