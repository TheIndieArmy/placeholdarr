import { useEffect } from "react";
import type { ThemeMode } from "../../brandTypes";
import type { DeterminationExplainResponse } from "../../types/api";
import { ExplainStatusIcon } from "../../components/explain/ExplainStatusIcon";
import { formatDeterminationLabel } from "./detailFormatters";

function modalTitleForDetermination(determination: string | null | undefined): string {
  const label = formatDeterminationLabel(determination);
  if (!label || label === "—") return "Why this determination?";
  return `Why ${label}?`;
}

export function DeterminationExplainModal(props: {
  open: boolean;
  onClose: () => void;
  result: DeterminationExplainResponse | null;
  loading: boolean;
  error: string | null;
  determination?: string | null;
  themeMode: ThemeMode;
}) {
  const isLight = props.themeMode === "light";

  useEffect(() => {
    if (!props.open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") props.onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.open, props.onClose]);

  if (!props.open) return null;

  const title = modalTitleForDetermination(props.determination ?? props.result?.determination);

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
      onClick={(event) => {
        if (event.target === event.currentTarget) props.onClose();
      }}
    >
      <div
        className={`w-full max-w-lg rounded-lg border shadow-lg shadow-black/15 overflow-hidden ${
          isLight ? "border-slate-200 bg-white" : "border-[#424753]/40 bg-[#171c22]"
        }`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="determination-explain-title"
      >
        <div className={`px-5 py-4 border-b ${isLight ? "border-slate-200" : "border-[#424753]/30"}`}>
          <h3
            id="determination-explain-title"
            className={`text-[18px] font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}
          >
            {title}
          </h3>
          {props.result?.title ? (
            <p className={`mt-1 text-[14px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
              {props.result.title}
            </p>
          ) : null}
          {props.result?.summary ? (
            <p className={`mt-2 text-[14px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-300"}`}>
              {props.result.summary}
            </p>
          ) : null}
        </div>

        <div className="px-5 py-4 max-h-[min(60vh,420px)] overflow-y-auto">
          {props.loading ? (
            <p className={`text-[14px] ${isLight ? "text-slate-500" : "text-slate-400"}`}>Loading…</p>
          ) : null}
          {props.error ? <p className="text-[14px] text-red-400">{props.error}</p> : null}
          {props.result ? (
            <div className="flex flex-col gap-2">
              {props.result.steps.map((step) => {
                const highlighted = step.key === props.result?.deciding_step_key;
                return (
                  <div
                    key={step.key}
                    className={`rounded-lg border px-3 py-2 ${
                      highlighted
                        ? isLight
                          ? "border-sky-200 bg-sky-50"
                          : "border-sky-500/30 bg-sky-500/10"
                        : isLight
                          ? "border-slate-200 bg-slate-50"
                          : "border-[#424753]/40 bg-[#1e2430]/40"
                    }`}
                  >
                    <div className="flex items-start gap-2">
                      <ExplainStatusIcon status={step.status} />
                      <div className="min-w-0 flex-1">
                        <div
                          className={`text-[13px] font-medium ${
                            isLight ? "text-slate-800" : "text-slate-200"
                          }`}
                        >
                          {step.label}
                        </div>
                        {step.detail ? (
                          <p className={`mt-0.5 text-[12px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-400"}`}>
                            {step.detail}
                          </p>
                        ) : null}
                        {step.outcome ? (
                          <p className={`mt-1 text-[11px] font-headline uppercase tracking-wider ${isLight ? "text-slate-500" : "text-slate-500"}`}>
                            Outcome: {formatDeterminationLabel(step.outcome)}
                          </p>
                        ) : null}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>

        <div
          className={`px-5 py-4 flex justify-end border-t ${
            isLight ? "border-slate-200" : "border-[#424753]/30"
          }`}
        >
          <button
            type="button"
            onClick={props.onClose}
            className={`text-[14px] font-headline uppercase tracking-wider ${
              isLight ? "text-slate-500 hover:text-slate-900" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
