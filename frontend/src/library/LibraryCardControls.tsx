import type { ThemeMode } from "../brandTypes";
import type { LibraryCardAccent } from "./LibraryGridCard";
import {
  LIBRARY_CARD_SIZE_MAX,
  LIBRARY_CARD_SIZE_MIN,
  LIBRARY_CARD_VARIANT_META,
  type LibraryCardSettings,
  type LibraryCardVariant,
  type LibraryViewMode,
} from "./cardSettings";

export function LibraryCardControls(props: {
  settings: LibraryCardSettings;
  onChange: (next: LibraryCardSettings) => void;
  accent: LibraryCardAccent;
  themeMode: ThemeMode;
  onOpenPreview?: () => void;
  compact?: boolean;
}) {
  const accent = props.accent;
  const isLight = props.themeMode === "light";

  const setVariant = (variant: LibraryCardVariant) => {
    props.onChange({ ...props.settings, variant });
  };

  const setSize = (posterWidthPx: number) => {
    props.onChange({ ...props.settings, posterWidthPx });
  };

  const setViewMode = (viewMode: LibraryViewMode) => {
    props.onChange({ ...props.settings, viewMode });
  };

  const isList = props.settings.viewMode === "list";

  return (
    <div
      className={`flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 ${
        isLight ? "bg-white border-slate-200/90 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
      } ${props.compact ? "text-[13px]" : ""}`}
    >
      <div className="flex items-center gap-2 min-w-[140px]">
        <span className={`material-symbols-outlined ${isLight ? "text-slate-500" : "text-slate-400"}`} style={{ fontSize: 18 }}>
          photo_size_select_large
        </span>
        <label className={`font-headline uppercase tracking-wider text-[12px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
          {isList ? "Row size" : "Card size"}
        </label>
      </div>
      <input
        type="range"
        min={LIBRARY_CARD_SIZE_MIN}
        max={LIBRARY_CARD_SIZE_MAX}
        step={4}
        value={props.settings.posterWidthPx}
        onChange={(e) => setSize(Number(e.target.value))}
        className="w-28 sm:w-36 accent-[color:var(--brand-accent)]"
        style={{ ["--brand-accent" as string]: accent.hex }}
        aria-label="Library card size"
      />
      <span className={`tabular-nums text-[12px] font-mono min-w-[3rem] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
        {props.settings.posterWidthPx}px
      </span>

      <div className={`hidden sm:block w-px h-6 ${isLight ? "bg-slate-200" : "bg-[#424753]/50"}`} aria-hidden />

      <div className="flex gap-0.5 rounded-md border p-0.5" style={{ borderColor: isLight ? "#e2e8f0" : "rgba(66,71,83,0.5)" }}>
        {(
          [
            { id: "grid" as const, icon: "grid_view", label: "Grid" },
            { id: "list" as const, icon: "view_list", label: "List" },
          ] as const
        ).map((mode) => {
          const active = props.settings.viewMode === mode.id;
          return (
            <button
              key={mode.id}
              type="button"
              onClick={() => setViewMode(mode.id)}
              className={`inline-flex items-center gap-1 px-2.5 py-1 rounded font-headline uppercase tracking-wider text-[12px] transition-colors ${
                active
                  ? isLight
                    ? "text-slate-900 font-semibold"
                    : "text-white font-semibold"
                  : isLight
                    ? "text-slate-600 hover:text-slate-900"
                    : "text-slate-400 hover:text-slate-200"
              }`}
              style={active ? { backgroundColor: accent.hex } : undefined}
              aria-pressed={active}
              title={`${mode.label} view`}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
                {mode.icon}
              </span>
              {mode.label}
            </button>
          );
        })}
      </div>

      {!isList ? (
        <>
          <div className={`hidden sm:block w-px h-6 ${isLight ? "bg-slate-200" : "bg-[#424753]/50"}`} aria-hidden />

          <div className="flex flex-wrap gap-1">
            {(Object.keys(LIBRARY_CARD_VARIANT_META) as LibraryCardVariant[]).map((id) => {
          const active = props.settings.variant === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => setVariant(id)}
              className={`px-3 py-1 rounded-md font-headline uppercase tracking-wider text-[12px] transition-colors ${
                active
                  ? isLight
                    ? "text-slate-900 font-semibold"
                    : "text-white font-semibold"
                  : isLight
                    ? "text-slate-600 hover:text-slate-900 hover:bg-slate-50"
                    : "text-slate-400 hover:text-slate-200"
              }`}
              style={active ? { backgroundColor: accent.hex } : undefined}
              title={LIBRARY_CARD_VARIANT_META[id].description}
            >
              {LIBRARY_CARD_VARIANT_META[id].label}
            </button>
          );
            })}
          </div>
        </>
      ) : null}

      {props.onOpenPreview ? (
        <button
          type="button"
          onClick={props.onOpenPreview}
          className={`ml-auto text-[12px] font-headline uppercase tracking-wider underline-offset-2 hover:underline ${
            isLight ? "text-slate-600 hover:text-slate-900" : "text-slate-400 hover:text-slate-200"
          }`}
        >
          Compare all styles →
        </button>
      ) : null}
    </div>
  );
}
