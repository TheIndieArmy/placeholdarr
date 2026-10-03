import { useState, type ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";
import type { SeriesEpisodeDetail } from "../../types/api";
import { detailMutedChipClass, detailStatusChipClass, unresolvedDetailStatus } from "./detailFormatters";
import { DeterminationWhyLink } from "./DeterminationWhyLink";
import { PlaceholderPolicyCycle, type PolicySyncPhase } from "./PlaceholderPolicyCycle";
import {
  densityLocksEpisodePins,
  densityPolicyUnavailableReason,
} from "./placeholderPolicyUtils";

export function EpisodeRow(props: {
  episode: SeriesEpisodeDetail;
  themeMode: ThemeMode;
  accentHex: string;
  /** Global TV density; greys episode pins under season/series modes. */
  tvPlaceholderDensity?: string | null;
  refreshControl?: ReactNode;
  onPolicyApplied?: () => void;
}) {
  const isLight = props.themeMode === "light";
  const ep = props.episode;
  const [open, setOpen] = useState(false);
  const [policyPhase, setPolicyPhase] = useState<PolicySyncPhase>("idle");
  const hasOverview = Boolean(ep.overview?.trim());
  const seriesGateLocked = Boolean(ep.policy_locked);
  const densityLocked = densityLocksEpisodePins(props.tvPlaceholderDensity);
  const episodeLocked = seriesGateLocked || densityLocked;
  const episodeLockedReason = seriesGateLocked
    ? "Set by series. Change the series chip to unlock."
    : densityPolicyUnavailableReason("episode", props.tvPlaceholderDensity) || undefined;

  const policyBusy = policyPhase === "creating" || policyPhase === "removing";
  let statusLabel: string;
  let statusKind: "file" | "placeholder" | "missing" | "not_needed";
  if (policyPhase === "creating") {
    statusLabel = "Creating…";
    statusKind = "placeholder";
  } else if (policyPhase === "removing") {
    statusLabel = "Removing…";
    statusKind = "missing";
  } else if (ep.has_placeholder) {
    statusLabel = "Placeholder";
    statusKind = "placeholder";
  } else if (ep.has_file) {
    statusLabel = "Downloaded";
    statusKind = "file";
  } else {
    const unresolved = unresolvedDetailStatus(ep.determination);
    statusLabel = unresolved.label;
    statusKind = unresolved.kind;
  }

  const statusClass = `${detailStatusChipClass(isLight, statusKind)}${
    !policyBusy && ep.determination
      ? isLight
        ? " cursor-pointer hover:border-sky-300 hover:bg-sky-50"
        : " cursor-pointer hover:border-sky-500/50 hover:bg-sky-500/10"
      : ""
  }`;

  const statusChip =
    !policyBusy && ep.determination ? (
      <DeterminationWhyLink
        mediaType="episode"
        entityId={ep.id}
        determination={ep.determination}
        themeMode={props.themeMode}
        className={statusClass}
        title="Why this status?"
      >
        {statusLabel}
      </DeterminationWhyLink>
    ) : (
      <span className={statusClass}>{statusLabel}</span>
    );

  return (
    <div className={`border-t ${isLight ? "border-slate-200" : "border-[#424753]/15"}`}>
      <div className={`flex items-start gap-4 px-5 py-3 ${isLight ? "hover:bg-slate-50" : "hover:bg-[#1e2430]/30"}`}>
        <div className="flex-none w-16 h-10 rounded overflow-hidden bg-[#1e2430] border border-[#424753]/40">
          {ep.still_url ? <img src={ep.still_url} alt="" className="w-full h-full object-cover" /> : null}
        </div>
        <span className="flex-none w-8 text-[14px] text-slate-500 font-mono pt-0.5">
          E{String(ep.episode_number).padStart(2, "0")}
        </span>
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
            <p className={`mt-2 text-[14px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-400"}`}>
              {ep.overview}
            </p>
          ) : null}
        </div>
        <div className="flex flex-col items-end gap-1 flex-none">
          {ep.sonarr_quality ? <span className={detailMutedChipClass(isLight)}>{ep.sonarr_quality}</span> : null}
          {statusChip}
          {ep.sonarr_monitored === false ? (
            <span className="text-[10px] uppercase tracking-wider text-slate-500">Unmonitored</span>
          ) : null}
          <PlaceholderPolicyCycle
            mediaType="episode"
            entityId={ep.id}
            placeholderPolicy={ep.placeholder_policy}
            forcePlaceholder={ep.force_placeholder}
            blockPlaceholder={ep.block_placeholder}
            hasPlaceholder={ep.has_placeholder}
            hasFile={ep.has_file}
            locked={episodeLocked}
            lockedReason={episodeLockedReason}
            accentHex={props.accentHex}
            themeMode={props.themeMode}
            size="sm"
            showInlineProgress={false}
            onPhaseChange={setPolicyPhase}
            onApplied={props.onPolicyApplied}
          />
          {props.refreshControl}
        </div>
      </div>
    </div>
  );
}
