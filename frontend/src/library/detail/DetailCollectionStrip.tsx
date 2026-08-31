import { Link } from "react-router-dom";
import type { DetailCollectionMember } from "../../types/api";
import type { ThemeMode } from "../../brandTypes";
import { detailMutedChipClass, tmdbMovieUrl } from "./detailFormatters";

function memberStatusLabel(status: DetailCollectionMember["status"]): string {
  if (status === "downloaded") return "Downloaded";
  if (status === "placeholder") return "Placeholder";
  if (status === "missing") return "Missing";
  return "Not in library";
}

function memberKey(m: DetailCollectionMember): string {
  if (m.id != null) return `id:${m.id}`;
  if (m.tmdbid != null) return `tmdb:${m.tmdbid}`;
  return `${m.title}-${m.year ?? ""}`;
}

export function DetailCollectionStrip(props: {
  title: string;
  members: DetailCollectionMember[];
  collectionTotal?: number | null;
  themeMode: ThemeMode;
}) {
  const isLight = props.themeMode === "light";
  const members = props.members;
  const inLibrary = members.filter((m) => m.status !== "not_in_library").length;
  const downloaded = members.filter((m) => m.status === "downloaded").length;
  const placeholders = members.filter((m) => m.status === "placeholder").length;
  const total = props.collectionTotal ?? members.length;
  const mutedChip = detailMutedChipClass(isLight);

  return (
    <div
      className={`rounded-xl border p-6 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}
    >
      <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
        <div className="min-w-0">
          <div className="text-[13px] font-headline uppercase tracking-widest text-slate-500 mb-1">Collection</div>
          <div className={`text-[20px] font-semibold ${isLight ? "text-slate-900" : "text-white"}`}>{props.title}</div>
        </div>
        <div className={`text-[14px] tabular-nums ${isLight ? "text-slate-600" : "text-slate-300"}`}>
          <span className={`font-semibold ${isLight ? "text-slate-900" : "text-white"}`}>{inLibrary}</span>
          {" of "}
          {total} in library
          <span className="text-slate-500">
            {" · "}
            {downloaded} downloaded
            {" · "}
            {placeholders} placeholder{placeholders === 1 ? "" : "s"}
          </span>
        </div>
      </div>

      <div className="flex gap-4 overflow-x-auto pb-1 -mx-1 px-1">
        {members.map((m) => {
          const outside = m.status === "not_in_library";
          const libraryHref =
            !m.is_current && m.id != null && Number.isFinite(Number(m.id)) ? `/library/movie/${m.id}` : null;
          const externalHref = outside && !libraryHref ? tmdbMovieUrl(m.tmdbid) : null;
          const href = libraryHref || externalHref;
          const interactive = Boolean(href);

          const card = (
            <>
              <div
                className={`relative aspect-[2/3] rounded-lg overflow-hidden border ${
                  m.is_current
                    ? isLight
                      ? "border-amber-400 ring-2 ring-amber-400/40"
                      : "border-amber-400/80 ring-2 ring-amber-400/30"
                    : isLight
                      ? "border-slate-200 bg-slate-100"
                      : "border-[#424753]/50 bg-[#1e2430]"
                }`}
              >
                {m.poster_url ? (
                  <img src={m.poster_url} alt="" className={`w-full h-full object-cover ${outside ? "grayscale" : ""}`} />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-slate-500 text-[12px] font-headline uppercase tracking-wider px-2 text-center">
                    No art
                  </div>
                )}
                {m.is_current ? (
                  <div className="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded text-[10px] font-headline uppercase tracking-wider bg-amber-400 text-slate-900">
                    This title
                  </div>
                ) : null}
              </div>
              <div className={`mt-2 text-[13px] font-medium leading-snug line-clamp-2 ${isLight ? "text-slate-800" : "text-slate-100"}`}>
                {m.title}
                {m.year ? <span className="text-slate-500 font-normal"> ({m.year})</span> : null}
              </div>
              <div className="mt-1.5">
                <span className={mutedChip}>{memberStatusLabel(m.status)}</span>
              </div>
            </>
          );

          const wrapClass = `flex-none w-[7.5rem] ${outside ? "opacity-55" : ""} ${
            interactive ? (isLight ? "hover:opacity-90" : "hover:opacity-90") : ""
          } ${interactive ? "cursor-pointer transition-opacity" : ""}`;

          if (libraryHref) {
            return (
              <Link key={memberKey(m)} to={libraryHref} className={wrapClass}>
                {card}
              </Link>
            );
          }
          if (externalHref) {
            return (
              <a
                key={memberKey(m)}
                href={externalHref}
                target="_blank"
                rel="noreferrer"
                className={wrapClass}
                title="Open on TMDB"
              >
                {card}
              </a>
            );
          }
          return (
            <div key={memberKey(m)} className={wrapClass}>
              {card}
            </div>
          );
        })}
      </div>
    </div>
  );
}
