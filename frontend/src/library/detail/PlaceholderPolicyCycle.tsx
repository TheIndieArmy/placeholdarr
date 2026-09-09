import { useCallback, useEffect, useRef, useState } from "react";
import type { ThemeMode } from "../../brandTypes";
import {
  getEntityReconcileStatus,
  setEpisodePlaceholderPolicy,
  setMoviePlaceholderPolicy,
  setSeasonPlaceholderPolicy,
  setSeriesPlaceholderPolicy,
} from "../../api/dashboard";
import { PinGlyph, type PlaceholderPolicy } from "./PinGlyph";
import {
  nextPlaceholderPolicy,
  POLICY_LABEL,
  POLICY_TOOLTIP,
  policyFromFlags,
} from "./placeholderPolicyUtils";

/** Pause after last click before save so Auto → Never → Pinned can settle. */
const APPLY_DEBOUNCE_MS = 1100;

export type PolicySyncPhase = "idle" | "creating" | "removing" | "working" | "saved";
export type PolicyMediaType = "movie" | "episode" | "series" | "season";

async function waitForReconcileJob(jobId: number): Promise<void> {
  for (let i = 0; i < 80; i += 1) {
    const status = await getEntityReconcileStatus(jobId);
    if (status.status === "done") return;
    if (status.status === "failed") {
      throw new Error(status.error_message || "Sync failed");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error("Timed out waiting for placeholder sync");
}

function phaseForPolicy(
  policy: PlaceholderPolicy,
  hasPlaceholder: boolean,
  hasFile: boolean,
): PolicySyncPhase {
  if (hasFile) return "working";
  if (policy === "never" && hasPlaceholder) return "removing";
  if (policy === "pinned" && !hasPlaceholder) return "creating";
  return "working";
}

function setterForMediaType(mediaType: PolicyMediaType) {
  switch (mediaType) {
    case "movie":
      return setMoviePlaceholderPolicy;
    case "episode":
      return setEpisodePlaceholderPolicy;
    case "series":
      return setSeriesPlaceholderPolicy;
    case "season":
      return setSeasonPlaceholderPolicy;
  }
}

export function PlaceholderPolicyCycle(props: {
  mediaType: PolicyMediaType;
  entityId: number;
  placeholderPolicy?: PlaceholderPolicy | null;
  forcePlaceholder?: boolean;
  blockPlaceholder?: boolean;
  hasPlaceholder?: boolean;
  hasFile?: boolean;
  /** Option A: series gate greys and disables season/episode chips. */
  locked?: boolean;
  lockedReason?: string;
  accentHex: string;
  themeMode: ThemeMode;
  size?: "sm" | "md";
  /** When true, show Creating…/Removing… beside the pin (movie/series meta). Episodes use onPhaseChange. */
  showInlineProgress?: boolean;
  /**
   * Where the progress label sits relative to the chip.
   * Use "start" when the chip is right-aligned (season header) so the chip does not shift.
   * Use "end" when the chip is left-aligned (meta strip) so the chip does not shift.
   */
  inlineProgressSide?: "start" | "end";
  onPhaseChange?: (phase: PolicySyncPhase) => void;
  onApplied?: () => void;
}) {
  const isLight = props.themeMode === "light";
  const size = props.size ?? "md";
  const locked = Boolean(props.locked);
  const progressSide = props.inlineProgressSide ?? "end";
  const hasPlaceholder = Boolean(props.hasPlaceholder);
  const hasFile = Boolean(props.hasFile);
  const serverPolicy = policyFromFlags(
    props.forcePlaceholder,
    props.blockPlaceholder,
    props.placeholderPolicy,
  );

  const [displayPolicy, setDisplayPolicy] = useState<PlaceholderPolicy>(serverPolicy);
  const [phase, setPhase] = useState<PolicySyncPhase>("idle");
  const [error, setError] = useState<string | null>(null);

  /** Bumped on every click / save start so stale responses do not own the UI. */
  const generationRef = useRef(0);
  const displayPolicyRef = useRef<PlaceholderPolicy>(serverPolicy);
  /** Last policy the user settled on (debounced target). */
  const desiredPolicyRef = useRef<PlaceholderPolicy>(serverPolicy);
  /** Last policy confirmed written by a save that was still current when it finished. */
  const confirmedPolicyRef = useRef<PlaceholderPolicy>(serverPolicy);
  const inFlightRef = useRef(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savePolicyRef = useRef<(policy: PlaceholderPolicy) => Promise<void>>(async () => {});

  const updatePhase = useCallback(
    (next: PolicySyncPhase) => {
      setPhase(next);
      props.onPhaseChange?.(next);
    },
    [props.onPhaseChange],
  );

  useEffect(() => {
    // Only adopt server policy when we are not mid-edit / mid-save.
    if (inFlightRef.current) return;
    if (debounceRef.current) return;
    if (displayPolicyRef.current !== confirmedPolicyRef.current) return;
    setDisplayPolicy(serverPolicy);
    displayPolicyRef.current = serverPolicy;
    desiredPolicyRef.current = serverPolicy;
    confirmedPolicyRef.current = serverPolicy;
  }, [serverPolicy]);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
    };
  }, []);

  const savePolicy = useCallback(
    async (policy: PlaceholderPolicy) => {
      desiredPolicyRef.current = policy;
      const gen = ++generationRef.current;
      inFlightRef.current = true;
      updatePhase(phaseForPolicy(policy, hasPlaceholder, hasFile));
      setError(null);
      try {
        const setter = setterForMediaType(props.mediaType);
        const out = await setter(props.entityId, { policy });

        // Stale response may have overwritten a newer save on the server. Re-assert latest.
        if (gen !== generationRef.current || desiredPolicyRef.current !== policy) {
          const latest = desiredPolicyRef.current;
          if (latest !== policy) {
            await savePolicyRef.current(latest);
          } else {
            confirmedPolicyRef.current = policy;
            inFlightRef.current = false;
            updatePhase("saved");
            if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
            savedTimerRef.current = setTimeout(() => updatePhase("idle"), 1500);
            props.onApplied?.();
          }
          return;
        }

        if (!out.ok) {
          throw new Error(out.message || "Failed to update placeholder policy");
        }
        if (out.job_id != null) {
          await waitForReconcileJob(out.job_id);
        }
        if (gen !== generationRef.current || desiredPolicyRef.current !== policy) {
          const latest = desiredPolicyRef.current;
          if (latest !== policy) {
            await savePolicyRef.current(latest);
          } else {
            confirmedPolicyRef.current = policy;
            inFlightRef.current = false;
            updatePhase("saved");
            if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
            savedTimerRef.current = setTimeout(() => updatePhase("idle"), 1500);
            props.onApplied?.();
          }
          return;
        }
        if (out.followup_job_id != null) {
          await waitForReconcileJob(out.followup_job_id);
        }
        if (gen !== generationRef.current || desiredPolicyRef.current !== policy) {
          const latest = desiredPolicyRef.current;
          if (latest !== policy) {
            await savePolicyRef.current(latest);
          } else {
            confirmedPolicyRef.current = policy;
            inFlightRef.current = false;
            updatePhase("saved");
            if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
            savedTimerRef.current = setTimeout(() => updatePhase("idle"), 1500);
            props.onApplied?.();
          }
          return;
        }

        confirmedPolicyRef.current = policy;
        inFlightRef.current = false;
        updatePhase("saved");
        if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
        savedTimerRef.current = setTimeout(() => updatePhase("idle"), 1500);
        props.onApplied?.();
      } catch (err) {
        if (gen !== generationRef.current) {
          const latest = desiredPolicyRef.current;
          if (latest !== policy) {
            await savePolicyRef.current(latest);
          }
          return;
        }
        inFlightRef.current = false;
        updatePhase("idle");
        setDisplayPolicy(confirmedPolicyRef.current);
        displayPolicyRef.current = confirmedPolicyRef.current;
        desiredPolicyRef.current = confirmedPolicyRef.current;
        setError(err instanceof Error ? err.message : "Failed to update placeholder policy");
      }
    },
    [
      props.mediaType,
      props.entityId,
      props.onApplied,
      hasPlaceholder,
      hasFile,
      updatePhase,
    ],
  );

  savePolicyRef.current = savePolicy;

  const commitPolicy = useCallback(
    async (policy: PlaceholderPolicy) => {
      if (policy !== displayPolicyRef.current) return;
      // Skip only when already confirmed on the server and nothing is in flight.
      // Do not compare to props.serverPolicy: an older in-flight Never may already
      // have committed while props still show the previous value.
      if (policy === confirmedPolicyRef.current && !inFlightRef.current) {
        updatePhase("idle");
        return;
      }
      await savePolicy(policy);
    },
    [savePolicy, updatePhase],
  );

  const cycle = () => {
    if (locked) return;
    // Latest click wins: invalidate in-flight UI ownership; the save layer re-asserts.
    if (phase === "creating" || phase === "removing" || phase === "working") {
      generationRef.current += 1;
    }
    const next = nextPlaceholderPolicy(displayPolicyRef.current);
    displayPolicyRef.current = next;
    desiredPolicyRef.current = next;
    setDisplayPolicy(next);
    setError(null);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null;
      void commitPolicy(next);
    }, APPLY_DEBOUNCE_MS);
  };

  const pad = size === "sm" ? "px-2 py-0.5" : "px-2.5 py-1";
  const glyphSize = size === "sm" ? 16 : 18;
  const textSize = size === "sm" ? "text-[10px]" : "text-[11px]";

  let chipStyle: { borderColor?: string; color?: string; backgroundColor?: string } | undefined;
  if (displayPolicy === "never") {
    const danger = isLight ? "#b91c1c" : "#f87171";
    chipStyle = {
      borderColor: `${danger}66`,
      color: danger,
      backgroundColor: isLight ? "#fef2f2" : "rgba(248,113,113,0.12)",
    };
  } else if (displayPolicy === "pinned") {
    chipStyle = {
      borderColor: `${props.accentHex}66`,
      color: props.accentHex,
      backgroundColor: isLight ? `${props.accentHex}12` : `${props.accentHex}18`,
    };
  }

  const busy = phase === "creating" || phase === "removing" || phase === "working";
  const inlineLabel =
    props.showInlineProgress === false
      ? null
      : phase === "creating"
        ? "Creating…"
        : phase === "removing"
          ? "Removing…"
          : phase === "working"
            ? "Saving…"
            : phase === "saved"
              ? "Saved"
              : null;

  const title = locked
    ? props.lockedReason || "Set by series. Change the series chip to unlock."
    : POLICY_TOOLTIP[displayPolicy];

  const progressLabel = inlineLabel ? (
    <span
      className={`${textSize} font-headline uppercase tracking-wider text-slate-500`}
      aria-live="polite"
    >
      {inlineLabel}
    </span>
  ) : null;

  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex items-center gap-2">
        {progressSide === "start" ? progressLabel : null}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            cycle();
          }}
          disabled={locked}
          className={`inline-flex items-center gap-1.5 rounded-full border ${pad} ${textSize} font-headline uppercase tracking-wider ${
            locked
              ? "cursor-not-allowed opacity-45"
              : "cursor-pointer"
          } ${
            displayPolicy === "auto"
              ? isLight
                ? "border-slate-200 text-slate-600 bg-white"
                : "border-[#424753]/50 text-slate-400 bg-transparent"
              : ""
          } ${busy ? "opacity-90" : ""}`}
          style={displayPolicy !== "auto" ? chipStyle : undefined}
          title={title}
        >
          <PinGlyph
            policy={displayPolicy}
            size={glyphSize}
            accentHex={props.accentHex}
            themeMode={props.themeMode}
          />
          {POLICY_LABEL[displayPolicy]}
        </button>
        {progressSide === "end" ? progressLabel : null}
      </div>
      {error ? <p className="text-[11px] text-red-400 max-w-[16rem]">{error}</p> : null}
    </div>
  );
}
