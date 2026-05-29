import { useMemo, useState, type CSSProperties } from "react";
import type { LibraryItem } from "../types/api";
import type { ThemeMode } from "../brandTypes";
import type { LibraryCardAccent } from "./LibraryGridCard";
import { LibraryCardControls } from "./LibraryCardControls";
import { LibraryGridCard } from "./LibraryGridCard";
import {
  LIBRARY_CARD_PREVIEW_PATH,
  LIBRARY_CARD_VARIANT_META,
  libraryPosterGridItemClassName,
  libraryPosterGridItemStyle,
  libraryPosterGridStyle,
  readLibraryCardSettings,
  writeLibraryCardSettings,
  type LibraryCardSettings,
  type LibraryCardVariant,
} from "./cardSettings";

export function LibraryCardStylePreview(props: {
  items: LibraryItem[];
  accent: LibraryCardAccent;
  themeMode: ThemeMode;
  onBack: () => void;
}) {
  const accent = props.accent;
  const isLight = props.themeMode === "light";
  const [draft, setDraft] = useState<LibraryCardSettings>(() => readLibraryCardSettings());
  const [appliedVariant, setAppliedVariant] = useState<LibraryCardVariant>(() => readLibraryCardSettings().variant);

  const updateDraft = (next: LibraryCardSettings) => {
    setDraft(next);
    writeLibraryCardSettings(next);
  };

  const samples = useMemo(() => props.items.slice(0, 12), [props.items]);
  const variants = Object.keys(LIBRARY_CARD_VARIANT_META) as LibraryCardVariant[];

  function applyVariant(variant: LibraryCardVariant) {
    const next = { ...draft, variant };
    updateDraft(next);
    setAppliedVariant(variant);
  }

  return (
    <div className="max-w-[1600px] mx-auto">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <button
            type="button"
            onClick={props.onBack}
            className={`mb-2 inline-flex items-center gap-1 text-[13px] font-headline uppercase tracking-wider ${
              isLight ? "text-slate-600 hover:text-slate-900" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
              arrow_back
            </span>
            Back to library
          </button>
          <h2 className={`text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
            Library card styles
          </h2>
          <p className={`text-[16px] mt-1 max-w-2xl ${isLight ? "text-slate-600" : "text-slate-400"}`}>
            Five grid styles plus list view on Movies/TV. Pick the overall concept first; fine-tuning comes later. Use{" "}
            <strong className={isLight ? "text-slate-800" : "text-slate-200"}>Use this style</strong> or change the style
            pills on the library page. Switch to <strong className={isLight ? "text-slate-800" : "text-slate-200"}>List</strong>{" "}
            there for dense rows.
          </p>
          <p className={`text-[13px] mt-2 font-mono ${isLight ? "text-slate-500" : "text-slate-500"}`}>
            Preview URL: {LIBRARY_CARD_PREVIEW_PATH}
          </p>
        </div>
        <LibraryCardControls settings={draft} onChange={updateDraft} accent={props.accent} themeMode={props.themeMode} />
      </div>

      {samples.length === 0 ? (
        <div className={`rounded-xl border p-8 text-center ${isLight ? "border-slate-200 bg-white" : "border-[#424753]/40 bg-[#171c22]"}`}>
          <p className={isLight ? "text-slate-600" : "text-slate-400"}>Load the Movies or TV library first so we have titles to preview.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-4 gap-6">
          {variants.map((variant) => {
            const meta = LIBRARY_CARD_VARIANT_META[variant];
            const isActive = appliedVariant === variant;
            return (
              <section
                key={variant}
                className={`rounded-xl border p-4 flex flex-col ${
                  isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
                } ${isActive ? "ring-2 ring-offset-2" : ""}`}
                style={
                  isActive
                    ? ({
                        ringColor: accent.hex,
                        ["--tw-ring-offset-color" as string]: isLight ? "#f8fafc" : "#0f141a",
                      } as CSSProperties)
                    : undefined
                }
              >
                <div className="mb-3">
                  <h3 className={`text-[18px] font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
                    {meta.label}
                  </h3>
                  <p className={`text-[12px] font-headline uppercase tracking-wider mt-0.5 ${isLight ? "text-slate-500" : "text-slate-500"}`}>
                    {meta.tagline}
                  </p>
                  <p className={`text-[14px] mt-2 ${isLight ? "text-slate-600" : "text-slate-400"}`}>{meta.description}</p>
                </div>
                <div className="flex-1 w-full overflow-visible" style={libraryPosterGridStyle(draft.posterWidthPx)}>
                  {samples.slice(0, 6).map((item) => (
                    <div
                      key={`${variant}-${item.id}`}
                      className={libraryPosterGridItemClassName}
                      style={libraryPosterGridItemStyle()}
                    >
                      <LibraryGridCard
                        item={item}
                        variant={variant}
                        posterWidthPx={draft.posterWidthPx}
                        accent={props.accent}
                        themeMode={props.themeMode}
                        onClick={() => applyVariant(variant)}
                      />
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => applyVariant(variant)}
                  className={`mt-4 w-full py-2.5 rounded-lg font-headline uppercase tracking-wider text-[13px] font-semibold transition-colors ${
                    isActive
                      ? isLight
                        ? "text-slate-900"
                        : "text-white"
                      : isLight
                        ? "border border-slate-200 text-slate-700 hover:bg-slate-50"
                        : "border border-[#424753]/50 text-slate-300 hover:bg-[#1e2430]"
                  }`}
                  style={isActive ? { backgroundColor: accent.hex } : undefined}
                >
                  {isActive ? "Active style" : "Use this style"}
                </button>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
