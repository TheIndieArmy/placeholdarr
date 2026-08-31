import { useState, type ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";
import type { SeriesEpisodeDetail } from "../../types/api";
import { detailMutedChipClass, detailStatusChipClass } from "./detailFormatters";

export function EpisodeRow(props: {
  episode: SeriesEpisodeDetail;
  themeMode: ThemeMode;
  refreshControl?: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  const ep = props.episode;
  const [open, setOpen] = useState(false);
  const hasOverview = Boolean(ep.overview?.trim());

  return (
    <div className={`border-t ${isLight ? "border-slate-200" : "border-[#424753]/15"}`}>
      <div className={`flex items-start gap-4 px-5 py-3 ${isLight ? "hover:bg-slate-50" : "hover:bg-[#1e2430]/30"}`}>
        <div className="flex-none w-16 h-10 rounded overflow-hidden bg-[#1e2430] border border-[#424753]/40">
          {ep.still_url ? <img src={ep.still_url} alt="" className="w-full h-full object-cover" /> : null}
        </div>
        <span className="flex-none w-8 text-[14px] text-slate-500 font-mono pt-0.5">E{String(ep.episode_number).padStart(2, "0")}</span>
        <div className="flex-1 min-w-0">
          <button
            type="button"
            onClick={() => hasOverview && setOpen((v) => !v)}
            className={`text-left w-full ${hasOverview ? "cursor-pointer" : "cursor-default"}`}
            disabled={!hasOverview}
          >
            <div className={`text-[16px] font-medium ${isLight ? "text-slate-900" : "text-white"}`}>
              {ep.title || `Episode ${ep.episode_number}`}
            </div>
            <div className="ui-field-description-compact mt-0.5">{ep.air_date || "No air date"}</div>
          </button>
          {open && ep.overview ? (
            <p className={`mt-2 text-[14px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-400"}`}>{ep.overview}</p>
          ) : null}
        </div>
        <div className="flex flex-col items-end gap-1 flex-none">
          {ep.sonarr_quality ? (
            <span className={detailMutedChipClass(isLight)}>
              {ep.sonarr_quality}
            </span>
          ) : null}
          {ep.has_placeholder
            ? <span className={detailStatusChipClass(isLight, "placeholder")}>Placeholder</span>
            : ep.has_file
              ? <span className={detailStatusChipClass(isLight, "file")}>Downloaded</span>
              : <span className={detailStatusChipClass(isLight, "missing")}>Missing</span>}
          {ep.sonarr_monitored === false ? (
            <span className="text-[10px] uppercase tracking-wider text-slate-500">Unmonitored</span>
          ) : null}
          {props.refreshControl}
        </div>
      </div>
    </div>
  );
}
