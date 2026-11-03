import type { ThemeMode } from "../../brandTypes";
import type { LibraryWatchlistSection } from "../../types/api";

export function DetailWatchlistSection(props: {
  watchlist?: LibraryWatchlistSection | null;
  libraryTitle: string;
  themeMode: ThemeMode;
}) {
  const isLight = props.themeMode === "light";
  const section = props.watchlist;
  const users = section?.users || [];
  const aliases = section?.alias_titles || [];
  const cardClass = isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40";
  const muted = isLight ? "text-slate-500" : "text-slate-400";
  const strong = isLight ? "text-slate-900" : "text-white";

  return (
    <div className={`rounded-xl border p-6 ${cardClass}`}>
      <div className="text-[13px] font-headline uppercase tracking-widest text-slate-500 mb-4">Watchlist</div>
      {!users.length ? (
        <p className={`text-[15px] ${muted}`}>Nobody on a synced Plex Watchlist has this title.</p>
      ) : (
        <div className="space-y-3">
          {aliases.length ? (
            <p className={`text-[13px] ${muted}`}>
              Also watchlisted as{" "}
              <span className={strong}>{aliases.join(", ")}</span>
            </p>
          ) : null}
          <ul className="space-y-2">
            {users.map((user) => {
              const label = user.title || user.username || user.plex_user_id;
              const initial = String(label || "?").trim().charAt(0).toUpperCase() || "?";
              return (
                <li key={user.plex_user_id} className="flex items-start gap-3 min-w-0">
                  {user.thumb_url ? (
                    <img
                      src={user.thumb_url}
                      alt=""
                      className="mt-0.5 h-8 w-8 shrink-0 rounded-full object-cover"
                    />
                  ) : (
                    <span
                      className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[12px] font-headline ${
                        isLight ? "bg-slate-200 text-slate-700" : "bg-[#252e3a] text-slate-200"
                      }`}
                    >
                      {initial}
                    </span>
                  )}
                  <div className="min-w-0">
                    <div className={`text-[15px] font-medium truncate ${strong}`}>{label}</div>
                    {user.watchlisted_as && user.watchlisted_as !== props.libraryTitle ? (
                      <div className={`text-[12px] mt-0.5 truncate ${muted}`}>
                        Watchlisted as {user.watchlisted_as}
                      </div>
                    ) : (
                      <div className={`text-[12px] mt-0.5 ${muted}`}>On watchlist</div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
