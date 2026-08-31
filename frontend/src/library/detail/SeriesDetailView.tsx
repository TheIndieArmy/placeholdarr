import { useMemo, useState } from "react";
import type { Brand, ThemeMode } from "../../brandTypes";
import type { SeriesDetailResponse } from "../../types/api";
import { refreshEpisodePlaceholder } from "../../api/dashboard";
import { LibraryReconcileControl } from "../LibraryReconcileContext";
import { DetailFactCard, DetailFactRow } from "./DetailFactCard";
import { DetailHero } from "./DetailHero";
import { DetailMetaStrip } from "./DetailMetaStrip";
import { EpisodeRow } from "./EpisodeRow";
import { SeriesFileStateSection } from "./FileStateSections";
import {
  detailMutedChipClass,
  formatMonitoredLabel,
  formatSonarrStatusLabel,
  imdbUrl,
  tmdbTvUrl,
} from "./detailFormatters";

export function SeriesDetailView(props: {
  payload: SeriesDetailResponse;
  brand: Brand;
  themeMode: ThemeMode;
  accent: { hex: string; icon: string; label: string };
}) {
  const payload = props.payload;
  const isLight = props.themeMode === "light";
  const [openSeasons, setOpenSeasons] = useState<number[]>([payload.seasons?.[0]?.id ?? 0].filter(Boolean));
  const seasonsDesc = useMemo(
    () => [...(payload.seasons || [])].sort((a, b) => (b.season_number || 0) - (a.season_number || 0)),
    [payload.seasons],
  );
  const stats = payload.episode_stats;
  const mutedChip = detailMutedChipClass(isLight);

  return (
    <div>
      <DetailHero
        title={payload.title}
        year={payload.year}
        posterUrl={payload.poster_url}
        backdropUrl={payload.backdrop_url}
        posterFallback="TV"
        accent={props.accent}
        themeMode={props.themeMode}
      />
      <div className="px-6 md:px-10 lg:px-12 pb-10">
        {payload.overview ? (
          <p className={`text-[18px] leading-relaxed max-w-3xl mb-6 ${isLight ? "text-slate-700" : "text-slate-200"}`}>
            {payload.overview}
          </p>
        ) : null}

        <DetailMetaStrip
          genres={payload.genres}
          runtime={payload.runtime}
          certification={payload.certification}
          ratings={payload.ratings_display}
          monitored={payload.sonarr_monitored}
          is4k={payload.is_4k}
          network={payload.network}
          networkLogoUrl={payload.network_logo_url}
          imdbid={payload.imdbid}
          tmdbTvId={payload.tmdb_id}
          accentHex={props.accent.hex}
          themeMode={props.themeMode}
        />

        <SeriesFileStateSection
          seasons={payload.seasons}
          links={payload.arr_instance_links}
          arrLink={payload.arr_link}
          instanceLabel={payload.instance_label}
          monitored={payload.sonarr_monitored}
          isLight={isLight}
          brandLabel={props.accent.label}
        />

        <div className="space-y-6">
          {payload.actors?.length ? (
            <div
              className={`rounded-xl border p-6 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}
            >
              <div className="text-[13px] font-headline uppercase tracking-widest text-slate-500 mb-4">Cast</div>
              <div className="flex flex-wrap gap-2.5">
                {payload.actors.map((a) => (
                  <span
                    key={a.name}
                    className={`px-3.5 py-2 rounded-lg text-[15px] border ${isLight ? "border-slate-200 bg-slate-50 text-slate-800" : "border-[#424753]/50 bg-[#1e2430] text-slate-200"}`}
                  >
                    <span className="font-medium">{a.name}</span>
                    {a.role ? <span className="text-slate-500"> · {a.role}</span> : null}
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          <div>
            <h3 className="text-[14px] font-headline uppercase tracking-widest text-slate-500 mb-3">Seasons & Episodes</h3>
            {stats ? (
              <div
                className={`rounded-xl border px-6 py-6 mb-3 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}
              >
                <div className="flex flex-wrap items-end gap-x-8 gap-y-4">
                  {[
                    { n: stats.files, label: "downloaded", strong: true },
                    { n: stats.placeholders, label: "placeholders", strong: true },
                    { n: stats.missing, label: "missing", strong: true },
                    { n: stats.future, label: "future", strong: false },
                    { n: stats.total, label: "total", strong: false },
                  ].map((item) => (
                    <div key={item.label} className="min-w-[4.5rem]">
                      <div
                        className={`text-[28px] font-headline font-bold tabular-nums leading-none ${
                          item.strong
                            ? isLight
                              ? "text-slate-900"
                              : "text-white"
                            : isLight
                              ? "text-slate-500"
                              : "text-slate-400"
                        }`}
                      >
                        {item.n}
                      </div>
                      <div className="mt-1.5 text-[13px] font-headline uppercase tracking-wider text-slate-500">
                        {item.label}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            <div className="space-y-2">
              {seasonsDesc.map((season) => {
                const open = openSeasons.includes(season.id);
                return (
                  <div
                    key={season.id}
                    className={`border rounded-xl overflow-hidden ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}
                  >
                    <button
                      type="button"
                      onClick={() =>
                        setOpenSeasons((prev) =>
                          prev.includes(season.id) ? prev.filter((id) => id !== season.id) : [...prev, season.id],
                        )
                      }
                      className={`w-full flex items-center justify-between px-5 py-4 text-left transition-colors ${isLight ? "hover:bg-slate-100" : "hover:bg-[#1e2430]/50"}`}
                    >
                      <div className="flex items-center gap-3">
                        {season.poster_url ? (
                          <img src={season.poster_url} alt="" className="w-10 h-10 rounded object-cover" />
                        ) : null}
                        <span className="material-symbols-outlined text-slate-500 transition-transform" style={{ fontSize: 18, transform: open ? "rotate(90deg)" : "rotate(0deg)" }}>
                          chevron_right
                        </span>
                        <span className={`text-[16px] font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
                          {season.season_number === 0 ? "Specials" : `Season ${season.season_number}`}
                        </span>
                        {season.monitored === false ? (
                          <span className="text-[10px] uppercase tracking-wider text-slate-500">Unmonitored</span>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-2 text-[12px] font-headline uppercase tracking-wider">
                        <span className="text-slate-500">{season.episode_total} eps</span>
                        <span className={mutedChip}>PH {season.episode_placeholders}</span>
                        <span className={mutedChip}>DL {season.episode_files}</span>
                      </div>
                    </button>
                    {open ? (
                      <div>
                        {season.episodes.map((ep) => (
                          <EpisodeRow
                            key={ep.id}
                            episode={ep}
                            themeMode={props.themeMode}
                            refreshControl={
                              <LibraryReconcileControl
                                label="Refresh"
                                startReconcile={() => refreshEpisodePlaceholder(ep.id)}
                                buttonClassName="text-[11px] uppercase tracking-wider text-slate-400 hover:text-slate-200 disabled:opacity-50"
                              />
                            }
                          />
                        ))}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>

          <div>
            <h3 className="text-[14px] font-headline uppercase tracking-widest text-slate-500 mb-3">Technical details</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              <DetailFactCard title="In Sonarr" themeMode={props.themeMode}>
                <DetailFactRow label="Status" value={formatSonarrStatusLabel(payload.sonarr_status)} themeMode={props.themeMode} />
                <DetailFactRow label="Monitored" value={formatMonitoredLabel(payload.sonarr_monitored)} themeMode={props.themeMode} />
              </DetailFactCard>
              <DetailFactCard title="Airing" themeMode={props.themeMode}>
                <DetailFactRow label="Premiere" value={payload.first_aired} themeMode={props.themeMode} />
                <DetailFactRow label="Last aired" value={payload.last_aired_date} themeMode={props.themeMode} />
              </DetailFactCard>
              <DetailFactCard title="External IDs" themeMode={props.themeMode}>
                <DetailFactRow
                  label="TMDB"
                  value={payload.tmdb_id != null ? String(payload.tmdb_id) : null}
                  href={tmdbTvUrl(payload.tmdb_id, payload.tvdbid)}
                  themeMode={props.themeMode}
                />
                <DetailFactRow
                  label="TVDB"
                  value={payload.tvdbid != null ? String(payload.tvdbid) : null}
                  href={tmdbTvUrl(null, payload.tvdbid)}
                  themeMode={props.themeMode}
                />
                <DetailFactRow
                  label="IMDB"
                  value={payload.imdbid}
                  href={imdbUrl(payload.imdbid)}
                  themeMode={props.themeMode}
                />
              </DetailFactCard>
              <DetailFactCard title="Catalog" themeMode={props.themeMode}>
                <DetailFactRow label="Indexed" value={payload.created_at} themeMode={props.themeMode} />
                <DetailFactRow label="Updated" value={payload.updated_at} themeMode={props.themeMode} />
              </DetailFactCard>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
