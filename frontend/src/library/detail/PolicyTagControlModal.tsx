import { useEffect, useState } from "react";
import type { ThemeMode } from "../../brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";
import type { PolicyTagControl } from "../../types/api";

export function PolicyTagControlModal(props: {
  mode: "tag" | "conflict";
  control: PolicyTagControl;
  arrLabel: "Radarr" | "Sonarr";
  accentHex: string;
  themeMode: ThemeMode;
  busy?: boolean;
  error?: string | null;
  onClose: () => void;
  onClear: (opts: { removeNever: boolean; removePinned: boolean }) => void;
}) {
  const isLight = props.themeMode === "light";
  const neverTags = props.control.matching_never_tags || [];
  const pinnedTags = props.control.matching_pinned_tags || [];
  const conflict = props.mode === "conflict" || Boolean(props.control.conflict);

  // Unchecked by default: checked means "remove this group from Arr".
  const [removeNever, setRemoveNever] = useState(false);
  const [removePinned, setRemovePinned] = useState(false);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !props.busy) props.onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.onClose, props.busy]);

  const canSubmit =
    (removeNever && neverTags.length > 0) || (removePinned && pinnedTags.length > 0);

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
      onClick={(event) => {
        if (event.target === event.currentTarget && !props.busy) props.onClose();
      }}
    >
      <div
        className={`w-full max-w-md rounded-lg border shadow-lg shadow-black/15 overflow-hidden ${
          isLight ? "border-slate-200 bg-white" : "border-[#424753]/40 bg-[#171c22]"
        }`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="policy-tag-modal-title"
      >
        <div className={`px-5 py-4 border-b ${isLight ? "border-slate-200" : "border-[#424753]/30"}`}>
          <h3
            id="policy-tag-modal-title"
            className={`text-[18px] font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}
          >
            {conflict ? "Conflicting policy tags" : "Controlled by Arr tag"}
          </h3>
          <p className={`mt-2 text-[15px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-300"}`}>
            {conflict
              ? `This title has both Never and Pinned policy tags in ${props.arrLabel}. Placeholdarr defaults to Never when both are present.`
              : `An Arr tag is setting this placeholder policy in ${props.arrLabel}.`}
          </p>
          <p className={`mt-2 text-[14px] leading-relaxed ${isLight ? "text-slate-500" : "text-slate-400"}`}>
            Check each group you want to remove from {props.arrLabel}, then click Clear in Arr. Leave a box unchecked
            to keep those tags.
          </p>

          <div className="mt-4 space-y-3">
            <div className={`text-[12px] font-headline uppercase tracking-widest ${isLight ? "text-slate-500" : "text-slate-500"}`}>
              Remove from {props.arrLabel}
            </div>
            {neverTags.length ? (
              <label className={`flex items-start gap-3 ${props.busy ? "opacity-60" : "cursor-pointer"}`}>
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={removeNever}
                  disabled={props.busy}
                  onChange={(e) => setRemoveNever(e.target.checked)}
                />
                <span className="min-w-0">
                  <span className={`block text-[13px] font-headline uppercase tracking-wider ${isLight ? "text-slate-500" : "text-slate-400"}`}>
                    Never tags
                  </span>
                  <span className={`block text-[14px] font-mono ${isLight ? "text-slate-800" : "text-slate-200"}`}>
                    {neverTags.join(", ")}
                  </span>
                </span>
              </label>
            ) : null}
            {pinnedTags.length ? (
              <label className={`flex items-start gap-3 ${props.busy ? "opacity-60" : "cursor-pointer"}`}>
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={removePinned}
                  disabled={props.busy}
                  onChange={(e) => setRemovePinned(e.target.checked)}
                />
                <span className="min-w-0">
                  <span className={`block text-[13px] font-headline uppercase tracking-wider ${isLight ? "text-slate-500" : "text-slate-400"}`}>
                    Pinned tags
                  </span>
                  <span className={`block text-[14px] font-mono ${isLight ? "text-slate-800" : "text-slate-200"}`}>
                    {pinnedTags.join(", ")}
                  </span>
                </span>
              </label>
            ) : null}
          </div>

          {props.error ? (
            <p className="mt-3 text-[14px] text-red-400">{props.error}</p>
          ) : null}
        </div>
        <div
          className={`px-5 py-4 flex flex-wrap justify-end gap-3 border-t ${
            isLight ? "border-slate-200" : "border-[#424753]/30"
          }`}
        >
          <button
            type="button"
            disabled={props.busy}
            onClick={props.onClose}
            className={`text-[14px] font-headline uppercase tracking-wider disabled:opacity-50 ${
              isLight ? "text-slate-500 hover:text-slate-900" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={props.busy || !canSubmit}
            title={
              canSubmit
                ? undefined
                : "Check at least one tag group to remove"
            }
            onClick={() =>
              props.onClear({
                removeNever: removeNever && neverTags.length > 0,
                removePinned: removePinned && pinnedTags.length > 0,
              })
            }
            className={`px-4 py-2 rounded-lg text-[14px] font-headline uppercase tracking-wider disabled:opacity-50 ${FG_ON_ACCENT_TEXT_CLASS}`}
            style={accentFilledStyle(props.accentHex)}
          >
            {props.busy ? "Clearing…" : "Clear in Arr"}
          </button>
        </div>
      </div>
    </div>
  );
}
