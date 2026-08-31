import type { CSSProperties } from "react";
import type { ThemeMode } from "../../brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";
import type { DetailRatingDisplay } from "./detailFormatters";
import {
  formatRuntimeMinutes,
  formatRatingLabel,
  formatRatingValue,
  ratingPageUrl,
  youtubeTrailerUrl,
} from "./detailFormatters";
import imdbIcon from "../../assets/ratings/imdb.svg";
import tmdbIcon from "../../assets/ratings/tmdb.svg";
import rottenTomatoesIcon from "../../assets/ratings/rottentomatoes.svg";
import metacriticIcon from "../../assets/ratings/metacritic.svg";
import traktIcon from "../../assets/ratings/trakt.svg";

type RatingVisual = {
  icon: string;
  wellStyle: CSSProperties;
  iconClass: string;
};

function ratingVisual(source: string): RatingVisual | null {
  const key = String(source || "").trim().toLowerCase();
  if (key === "imdb") {
    return {
      icon: imdbIcon,
      wellStyle: {
        backgroundColor: "#F5C518",
        border: "2px solid rgba(245, 197, 24, 0.95)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.25)",
      },
      iconClass: "h-7 w-7 object-contain",
    };
  }
  if (key === "tmdb" || key === "themoviedb") {
    return {
      icon: tmdbIcon,
      wellStyle: {
        backgroundColor: "#1e2430",
        border: "2px solid rgba(1, 180, 228, 0.85)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
      },
      iconClass: "h-7 w-7 object-contain",
    };
  }
  if (key === "rottentomatoes" || key === "rotten_tomatoes") {
    return {
      icon: rottenTomatoesIcon,
      wellStyle: {
        backgroundColor: "#1e2430",
        border: "2px solid rgba(250, 50, 10, 0.85)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
      },
      iconClass: "h-7 w-7 object-contain",
    };
  }
  if (key === "metacritic") {
    return {
      icon: metacriticIcon,
      wellStyle: {
        backgroundColor: "#1e2430",
        border: "2px solid rgba(102, 204, 51, 0.85)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
      },
      iconClass: "h-7 w-7 object-contain",
    };
  }
  if (key === "trakt") {
    return {
      icon: traktIcon,
      wellStyle: {
        backgroundColor: "#1e2430",
        border: "2px solid rgba(237, 28, 36, 0.85)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
      },
      iconClass: "h-7 w-7 object-contain",
    };
  }
  return null;
}

function RatingMiniCard(props: {
  source: string;
  value: string;
  href?: string | null;
  isLight: boolean;
}) {
  const visual = ratingVisual(props.source);
  const label = formatRatingLabel(props.source);
  const scoreClass = `text-[22px] font-headline font-bold leading-none tabular-nums ${
    props.isLight ? "text-slate-900" : "text-white"
  }`;
  const titleClass = `text-[11px] font-headline uppercase tracking-wider ${
    props.isLight ? "text-slate-600" : "text-slate-400"
  }`;
  const tileClass = `flex min-w-[5.75rem] flex-1 flex-col items-center gap-2 px-3 py-3.5 text-center sm:min-w-[6.5rem] sm:max-w-[8.5rem] rounded-xl border ${
    props.isLight ? "border-slate-200 bg-slate-50" : "border-[#424753]/50 bg-[#1e2430]"
  } ${props.href ? "hover:opacity-90 transition-opacity" : ""}`;

  const body = (
    <>
      {visual ? (
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl" style={visual.wellStyle}>
          <img src={visual.icon} alt="" className={visual.iconClass} aria-hidden />
        </div>
      ) : (
        <div
          className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl text-[11px] font-headline font-bold ${
            props.isLight ? "bg-slate-200 text-slate-700" : "bg-[#171c22] text-slate-300"
          }`}
        >
          {label.slice(0, 3)}
        </div>
      )}
      <div className={scoreClass}>{props.value}</div>
      <div className={titleClass}>{label}</div>
    </>
  );

  if (props.href) {
    return (
      <a href={props.href} target="_blank" rel="noreferrer" className={tileClass} aria-label={`${label} ${props.value}`}>
        {body}
      </a>
    );
  }
  return (
    <div className={tileClass} role="group" aria-label={`${label} ${props.value}`}>
      {body}
    </div>
  );
}

export function DetailMetaStrip(props: {
  genres?: string[] | null;
  runtime?: number | null;
  certification?: string | null;
  ratings?: DetailRatingDisplay[] | null;
  monitored?: boolean | null;
  /** @deprecated Misleading in meta — Arr instance role, not file quality. Ignored. */
  is4k?: boolean;
  studio?: string | null;
  network?: string | null;
  networkLogoUrl?: string | null;
  trailerUrl?: string | null;
  imdbid?: string | null;
  tmdbid?: number | null;
  tmdbTvId?: number | null;
  accentHex: string;
  themeMode: ThemeMode;
}) {
  void props.is4k;
  void props.monitored;
  const isLight = props.themeMode === "light";
  const runtime = formatRuntimeMinutes(props.runtime);
  const chips: string[] = [...(props.genres ?? [])].filter(Boolean);
  const ratings = props.ratings ?? [];
  const ratingIds = { imdbid: props.imdbid, tmdbid: props.tmdbid, tmdbTvId: props.tmdbTvId };

  type Fact = {
    key: string;
    value: string;
    label: string;
    soft?: boolean;
    logoUrl?: string | null;
  };

  const facts: Fact[] = [];
  if (runtime) {
    facts.push({ key: "runtime", value: runtime, label: "Runtime" });
  }
  if (props.certification) {
    facts.push({ key: "cert", value: props.certification, label: "Rating" });
  }
  if (props.network || props.networkLogoUrl) {
    facts.push({
      key: "network",
      value: props.network || "Network",
      label: "Network",
      logoUrl: props.networkLogoUrl,
    });
  } else if (props.studio) {
    facts.push({ key: "studio", value: props.studio, label: "Studio" });
  }

  const trailerHref = youtubeTrailerUrl(props.trailerUrl);
  const valueClass = (soft?: boolean) =>
    `text-[26px] sm:text-[28px] font-headline font-bold leading-none tabular-nums ${
      soft
        ? isLight
          ? "text-slate-500"
          : "text-slate-400"
        : isLight
          ? "text-slate-900"
          : "text-white"
    }`;

  return (
    <div
      className={`rounded-xl border px-6 py-6 mb-6 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}
    >
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-5">
        <div className="flex flex-wrap items-end gap-x-8 gap-y-5 min-w-0">
          {facts.map((fact) => (
            <div key={fact.key} className="min-w-[4.5rem]">
              {fact.logoUrl ? (
                <img
                  src={fact.logoUrl}
                  alt={fact.value}
                  className="h-8 w-auto max-w-[100px] object-contain object-left"
                />
              ) : (
                <div className={valueClass(fact.soft)}>{fact.value}</div>
              )}
              <div className="mt-1.5 text-[13px] font-headline uppercase tracking-wider text-slate-500">
                {fact.label}
              </div>
            </div>
          ))}
        </div>
        {trailerHref ? (
          <a
            href={trailerHref}
            target="_blank"
            rel="noreferrer"
            className={`inline-flex items-center gap-2 px-4 py-2.5 rounded-lg text-[14px] font-headline uppercase tracking-wider shrink-0 ${FG_ON_ACCENT_TEXT_CLASS}`}
            style={accentFilledStyle(props.accentHex)}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>play_circle</span>
            Trailer
          </a>
        ) : null}
      </div>

      {ratings.length ? (
        <div className={`mt-5 pt-5 border-t ${isLight ? "border-slate-200" : "border-[#424753]/40"}`}>
          <div className="text-[12px] font-headline uppercase tracking-widest text-slate-500 mb-3">Scores</div>
          <div className="flex w-full flex-wrap items-stretch justify-start gap-2.5 sm:gap-3">
            {ratings.map((r) => (
              <RatingMiniCard
                key={r.source}
                source={r.source}
                value={formatRatingValue(r.source, r.value)}
                href={ratingPageUrl(r.source, ratingIds)}
                isLight={isLight}
              />
            ))}
          </div>
        </div>
      ) : null}

      {chips.length ? (
        <div className={`flex flex-wrap gap-2.5 mt-5 pt-5 border-t ${isLight ? "border-slate-200" : "border-[#424753]/40"}`}>
          {chips.map((g) => (
            <span
              key={g}
              className={`px-3.5 py-1.5 rounded-lg text-[14px] font-headline uppercase tracking-wider border ${
                isLight ? "border-slate-200 bg-slate-50 text-slate-700" : "border-[#424753]/50 bg-[#1e2430] text-slate-300"
              }`}
            >
              {g}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
