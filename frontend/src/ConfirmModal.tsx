import { useEffect } from "react";
import type { ThemeMode } from "./brandTypes";

export function ConfirmModal(props: {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  accentHex: string;
  themeMode: ThemeMode;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const isLight = props.themeMode === "light";
  const confirmLabel = props.confirmLabel ?? "Leave";
  const cancelLabel = props.cancelLabel ?? "Stay";

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") props.onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.onCancel]);

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
      onClick={(event) => {
        if (event.target === event.currentTarget) props.onCancel();
      }}
    >
      <div
        className={`w-full max-w-md rounded-lg border shadow-lg shadow-black/15 overflow-hidden ${
          isLight ? "border-slate-200 bg-white" : "border-[#424753]/40 bg-[#171c22]"
        }`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-modal-title"
      >
        <div className={`px-5 py-4 border-b ${isLight ? "border-slate-200" : "border-[#424753]/30"}`}>
          <h3
            id="confirm-modal-title"
            className={`text-[18px] font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}
          >
            {props.title}
          </h3>
          <p className={`mt-2 text-[15px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-300"}`}>
            {props.message}
          </p>
        </div>
        <div
          className={`px-5 py-4 flex flex-wrap justify-end gap-3 border-t ${
            isLight ? "border-slate-200" : "border-[#424753]/30"
          }`}
        >
          <button
            type="button"
            onClick={props.onCancel}
            className={`text-[14px] font-headline uppercase tracking-wider ${
              isLight ? "text-slate-500 hover:text-slate-900" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={props.onConfirm}
            className="px-4 py-2 rounded-lg text-[14px] font-headline uppercase tracking-wider text-[#0a0e14]"
            style={{ backgroundColor: props.accentHex }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
