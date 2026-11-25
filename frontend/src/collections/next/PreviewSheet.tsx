import { useEffect, type ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";
import { getCollectionTheme } from "../collectionTheme";

/** Full preview pulled up over the builder from the dock. */
export function PreviewSheet(props: {
  open: boolean;
  onClose: () => void;
  themeMode: ThemeMode;
  title: ReactNode;
  headerExtra?: ReactNode;
  children: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const { open, onClose } = props;

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[70] lg:left-64">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-[2px]" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        className={`dock-sheet-up absolute inset-x-0 bottom-0 flex h-[88vh] flex-col rounded-t-2xl border-t shadow-2xl ${
          isLight ? "border-slate-200 bg-slate-50" : "border-[#424753]/60 bg-[#12161d]"
        }`}
      >
        <div className={`flex items-center gap-3 border-b px-5 py-3 ${theme.divider}`}>
          <div className={`min-w-0 flex-1 truncate text-[16px] font-headline font-bold ${isLight ? "text-slate-900" : "text-white"}`}>
            {props.title}
          </div>
          {props.headerExtra}
          <button
            type="button"
            onClick={onClose}
            className={`flex items-center gap-1 rounded-lg border px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider ${
              isLight ? "border-slate-300 text-slate-700 hover:bg-white" : "border-[#424753] text-slate-200 hover:bg-[#1e2430]"
            }`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
              close_fullscreen
            </span>
            Close
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex max-w-[90rem] flex-col gap-5 px-5 py-5">{props.children}</div>
        </div>
      </div>
    </div>
  );
}
