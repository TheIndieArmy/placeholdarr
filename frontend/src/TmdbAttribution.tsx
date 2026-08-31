import tmdbLogo from "./assets/tmdb-blue-short.svg";
import { TMDB_API_NOTICE } from "./tmdbAttribution";

/** TMDB logos & attribution — https://www.themoviedb.org/about/logos-attribution */
export function TmdbAttribution(props: {
  /** Show required TMDB API legal notice. */
  api?: boolean;
  /** Note that poster/backdrop images may be sourced from TMDB. */
  posters?: boolean;
  compact?: boolean;
  mutedClass?: string;
  className?: string;
}) {
  const { api = false, posters = false, compact = false, mutedClass = "text-slate-500", className = "" } = props;
  if (!api && !posters) return null;

  const textSize = compact ? "text-[10px]" : "text-[11px]";
  const logoHeight = compact ? "h-3.5" : "h-4";

  return (
    <div className={`flex flex-col gap-1.5 ${className}`}>
      <a
        href="https://www.themoviedb.org/"
        target="_blank"
        rel="noopener noreferrer"
        className="w-fit opacity-90 transition-opacity hover:opacity-100"
        title="The Movie Database (TMDB)"
      >
        <img src={tmdbLogo} alt="The Movie Database (TMDB)" className={`${logoHeight} w-auto`} />
      </a>
      {api ? <p className={`${textSize} leading-snug ${mutedClass}`}>{TMDB_API_NOTICE}</p> : null}
      {posters ? (
        <p className={`${textSize} leading-snug ${mutedClass}`}>
          Poster images provided by{" "}
          <a
            href="https://www.themoviedb.org/"
            target="_blank"
            rel="noopener noreferrer"
            className="underline decoration-slate-500/50 underline-offset-2 hover:decoration-current"
          >
            TMDB
          </a>
          .
        </p>
      ) : null}
    </div>
  );
}
