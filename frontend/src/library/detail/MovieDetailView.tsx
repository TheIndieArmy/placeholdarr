import type { Brand, ThemeMode } from "../../brandTypes";
import type { MovieDetailResponse } from "../../types/api";
import { DetailCollectionStrip } from "./DetailCollectionStrip";
import { DetailFactCard, DetailFactRow } from "./DetailFactCard";
import { DetailHero } from "./DetailHero";
import { DetailMetaStrip } from "./DetailMetaStrip";
import { MovieFileStateSection } from "./FileStateSections";
import {
  formatDeterminationLabel,
  formatDisplayStatusLabel,
  formatFileSize,
  formatMonitoredLabel,
  imdbUrl,
  tmdbMovieUrl,
} from "./detailFormatters";

export function MovieDetailView(props: {
  payload: MovieDetailResponse;
  brand: Brand;
  themeMode: ThemeMode;
  accent: { hex: string; icon: string; label: string };
}) {
  const payload = props.payload;
  const isLight = props.themeMode === "light";

  return (
    <div>
      <DetailHero
        title={payload.title}
        year={payload.year}
        posterUrl={payload.poster_url}
        backdropUrl={payload.backdrop_url}
        posterFallback="MOV"
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
          monitored={payload.radarr_monitored}
          is4k={payload.is_4k}
          studio={payload.studio}
          trailerUrl={payload.trailer_url}
          imdbid={payload.imdbid}
          tmdbid={payload.tmdbid}
          accentHex={props.accent.hex}
          themeMode={props.themeMode}
        />

        <MovieFileStateSection
          links={payload.arr_instance_links}
          arrLink={payload.arr_link}
          hasFile={payload.has_file}
          hasPlaceholder={payload.has_placeholder}
          instanceLabel={payload.instance_label}
          monitored={payload.radarr_monitored}
          isLight={isLight}
          brandLabel={props.accent.label}
          accentHex={props.accent.hex}
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

          {payload.collection_title && payload.collection_members?.length ? (
            <DetailCollectionStrip
              title={payload.collection_title}
              members={payload.collection_members}
              collectionTotal={payload.collection_total}
              themeMode={props.themeMode}
            />
          ) : payload.collection_title ? (
            <div
              className={`rounded-xl border p-6 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}
            >
              <div className="text-[13px] font-headline uppercase tracking-widest text-slate-500 mb-2">Collection</div>
              <div className={`text-[20px] font-semibold ${isLight ? "text-slate-900" : "text-white"}`}>
                {payload.collection_title}
              </div>
            </div>
          ) : null}

          <div>
            <h3 className="text-[14px] font-headline uppercase tracking-widest text-slate-500 mb-3">Technical details</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              <DetailFactCard title="In Radarr" themeMode={props.themeMode}>
                <DetailFactRow label="Quality" value={payload.radarr_quality} themeMode={props.themeMode} />
                <DetailFactRow label="Monitored" value={formatMonitoredLabel(payload.radarr_monitored)} themeMode={props.themeMode} />
                <DetailFactRow label="Release" value={payload.radarr_release_status} themeMode={props.themeMode} />
                <DetailFactRow label="File on disk" value={payload.has_file ? "Yes" : "No"} themeMode={props.themeMode} />
                <DetailFactRow label="Radarr ID" value={payload.radarr_id != null ? String(payload.radarr_id) : null} themeMode={props.themeMode} />
              </DetailFactCard>
              <DetailFactCard title="Placeholdarr" themeMode={props.themeMode}>
                <DetailFactRow label="Determination" value={formatDeterminationLabel(payload.determination)} themeMode={props.themeMode} />
                <DetailFactRow label="Status" value={formatDisplayStatusLabel(payload.display_status)} themeMode={props.themeMode} />
                <DetailFactRow label="Placeholder path" value={payload.placeholder_filepath} themeMode={props.themeMode} />
                <DetailFactRow label="Last search" value={payload.last_search} themeMode={props.themeMode} />
                <DetailFactRow label="Indexed" value={payload.created_at} themeMode={props.themeMode} />
                <DetailFactRow label="Updated" value={payload.updated_at} themeMode={props.themeMode} />
              </DetailFactCard>
              <DetailFactCard title="Releases" themeMode={props.themeMode}>
                <DetailFactRow label="Theatrical" value={payload.theater_release_date} themeMode={props.themeMode} />
                <DetailFactRow label="Digital" value={payload.digital_release_date} themeMode={props.themeMode} />
                <DetailFactRow label="Physical" value={payload.physical_release_date} themeMode={props.themeMode} />
              </DetailFactCard>
              <DetailFactCard title="External IDs" themeMode={props.themeMode}>
                <DetailFactRow
                  label="TMDB"
                  value={payload.tmdbid != null ? String(payload.tmdbid) : null}
                  href={tmdbMovieUrl(payload.tmdbid)}
                  themeMode={props.themeMode}
                />
                <DetailFactRow
                  label="IMDB"
                  value={payload.imdbid}
                  href={imdbUrl(payload.imdbid)}
                  themeMode={props.themeMode}
                />
              </DetailFactCard>
              <DetailFactCard title="Paths" themeMode={props.themeMode}>
                <DetailFactRow label="Library path" value={payload.library_path} themeMode={props.themeMode} />
                <DetailFactRow label="File path" value={payload.file_path} themeMode={props.themeMode} />
                <DetailFactRow label="File size" value={formatFileSize(payload.file_size_bytes)} themeMode={props.themeMode} />
              </DetailFactCard>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
