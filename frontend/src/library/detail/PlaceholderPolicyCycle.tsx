import { useCallback, useEffect, useRef, useState } from "react";
import type { ThemeMode } from "../../brandTypes";
import {
  getEntityReconcileStatus,
  setEpisodePlaceholderPolicy,
  setMoviePlaceholderPolicy,
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

export function PlaceholderPolicyCycle(props: {
  mediaType: "movie" | "episode";
  entityId: number;
  placeholderPolicy?: PlaceholderPolicy | null;
  forcePlaceholder?: boolean;
  blockPlaceholder?: boolean;
  hasPlaceholder?: boolean;
  hasFile?: boolean;
  accentHex: string;
  themeMode: ThemeMode;
  size?: "sm" | "md";
  /** When true, show Creating…/Removing… beside the pin (movie meta). Episodes use onPhaseChange. */
  showInlineProgress?: boolean;
  onPhaseChange?: (phase: PolicySyncPhase) => void;
  onApplied?: () => void;
}) {
  const isLight = props.themeMode === "light";
  const size = props.size ?? "md";
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

  const generationRef = useRef(0);
  const displayPolicyRef = useRef<PlaceholderPolicy>(serverPolicy);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const updatePhase = useCallback(
    (next: PolicySyncPhase) => {
      setPhase(next);
      props.onPhaseChange?.(next);
    },
    [props.onPhaseChange],
  );

  useEffect(() => {
    setDisplayPolicy(serverPolicy);
    displayPolicyRef.current = serverPolicy;
  }, [serverPolicy]);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
    };
  }, []);

  const savePolicy = useCallback(
    async (policy: PlaceholderPolicy) => {
      const gen = ++generationRef.current;
      updatePhase(phaseForPolicy(policy, hasPlaceholder, hasFile));
      setError(null);
      try {
        const setter =
          props.mediaType === "movie" ? setMoviePlaceholderPolicy : setEpisodePlaceholderPolicy;
        const out = await setter(props.entityId, { policy });
        if (gen !== generationRef.current) return;
        if (!out.ok) {
          throw new Error(out.message || "Failed to update placeholder policy");
        }
        if (out.job_id != null) {
          await waitForReconcileJob(out.job_id);
        }
        if (gen !== generationRef.current) return;
        if (out.followup_job_id != null) {
          await waitForReconcileJob(out.followup_job_id);
        }
        if (gen !== generationRef.current) return;
        updatePhase("saved");
        if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
        savedTimerRef.current = setTimeout(() => updatePhase("idle"), 1500);
        props.onApplied?.();
      } catch (err) {
        if (gen !== generationRef.current) return;
        updatePhase("idle");
        setDisplayPolicy(serverPolicy);
        displayPolicyRef.current = serverPolicy;
        setError(err instanceof Error ? err.message : "Failed to update placeholder policy");
      }
    },
    [
      props.mediaType,
      props.entityId,
      props.onApplied,
      serverPolicy,
      hasPlaceholder,
      hasFile,
      updatePhase,
    ],
  );

  const commitPolicy = useCallback(
    async (policy: PlaceholderPolicy) => {
      if (policy !== displayPolicyRef.current) return;
      if (policy === serverPolicy) {
        updatePhase("idle");
        return;
      }
      await savePolicy(policy);
    },
    [savePolicy, serverPolicy, updatePhase],
  );

  const cycle = () => {
    // Latest click wins: abandon any in-flight wait so a mid-sync flip can settle.
    if (phase === "creating" || phase === "removing" || phase === "working") {
      generationRef.current += 1;
      updatePhase("idle");
    }
    const next = nextPlaceholderPolicy(displayPolicyRef.current);
    displayPolicyRef.current = next;
    setDisplayPolicy(next);
    setError(null);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
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

  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={cycle}
          className={`inline-flex items-center gap-1.5 rounded-full border ${pad} ${textSize} font-headline uppercase tracking-wider cursor-pointer ${
            displayPolicy === "auto"
              ? isLight
                ? "border-slate-200 text-slate-600 bg-white"
                : "border-[#424753]/50 text-slate-400 bg-transparent"
              : ""
          } ${busy ? "opacity-90" : ""}`}
          style={displayPolicy !== "auto" ? chipStyle : undefined}
          title={POLICY_TOOLTIP[displayPolicy]}
        >
          <PinGlyph
            policy={displayPolicy}
            size={glyphSize}
            accentHex={props.accentHex}
            themeMode={props.themeMode}
          />
          {POLICY_LABEL[displayPolicy]}
        </button>
        {inlineLabel ? (
          <span
            className={`${textSize} font-headline uppercase tracking-wider text-slate-500`}
            aria-live="polite"
          >
            {inlineLabel}
          </span>
        ) : null}
      </div>
      {error ? <p className="text-[11px] text-red-400 max-w-[16rem]">{error}</p> : null}
    </div>
  );
}
