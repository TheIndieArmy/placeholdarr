import { useCallback, useMemo, useRef, useState } from "react";
import type { LibraryItem } from "../types/api";
import type { ThemeMode } from "../brandTypes";
import { LibraryCardControls } from "./LibraryCardControls";
import { LibraryGridCard } from "./LibraryGridCard";
import { LibraryListRow } from "./LibraryListRow";
import {
  libraryListStyle,
  libraryPosterGridItemClassName,
  libraryPosterGridItemStyle,
  libraryPosterGridStyle,
  readLibraryCardSettings,
  writeLibraryCardSettings,
} from "./cardSettings";
import { titleSortLetter } from "./librarySort";

export type LibraryShelfFilter = "all" | "placeholders" | "future" | "missing";

export function LibraryPanel(props: {
  shelfTitle: string;
  items: LibraryItem[];
  catalogTotal: number;
  activeFilter: LibraryShelfFilter;
  onFilterChange: (value: LibraryShelfFilter) => void;
  onOpenDetail: (item: LibraryItem) => void;
  onOpenCardStylePreview: () => void;
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
}) {
  const accent = props.accent;
  const isLight = props.themeMode === "light";
  const [cardSettings, setCardSettings] = useState(readLibraryCardSettings);
  const sectionRefs = useRef<Record<string, HTMLDivElement | null>>({});

  const handleCardSettings = useCallback((next: ReturnType<typeof readLibraryCardSettings>) => {
    setCardSettings(next);
    writeLibraryCardSettings(next);
  }, []);

  const filters: Array<{ id: LibraryShelfFilter; label: string }> = [
    { id: "all", label: "All" },
    { id: "placeholders", label: "Placeholders" },
    { id: "future", label: "Future" },
    { id: "missing", label: "Missing" },
  ];
  const totalMissing = props.items.filter((i) => i.has_missing).length;

  const groupedItems = useMemo(() => {
    const groups: Record<string, LibraryItem[]> = {};
    props.items.forEach((item) => {
      const letter = titleSortLetter(item.title);
      if (!groups[letter]) groups[letter] = [];
      groups[letter].push(item);
    });
    const letters = Object.keys(groups).sort((a, b) => {
      if (a === "#") return 1;
      if (b === "#") return -1;
      return a.localeCompare(b);
    });
    return { groups, letters };
  }, [props.items]);

  return (
    <div>
      <div className="flex flex-wrap justify-between items-end gap-4 mb-4">
        <div>
          <h2 className={`text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>{props.shelfTitle}</h2>
          <p className={`text-[16px] mt-1 ${isLight ? "text-slate-600" : "text-slate-400"}`}>
            Showing {props.items.length} of {props.catalogTotal} items matching your criteria
          </p>
        </div>
        <div
          className={`flex flex-wrap gap-1 p-1 rounded-lg border ${
            isLight ? "bg-white border-slate-200/90 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          {filters.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => props.onFilterChange(f.id)}
              className={`px-4 py-1.5 rounded-md text-[14px] font-headline uppercase tracking-wider transition-colors ${
                f.id === props.activeFilter
                  ? isLight
                    ? "text-slate-900 font-semibold"
                    : "text-white font-semibold"
                  : isLight
                    ? "text-slate-600 hover:text-slate-900 hover:bg-slate-50"
                    : "text-slate-400 hover:text-slate-200"
              }`}
              style={f.id === props.activeFilter ? { backgroundColor: accent.hex } : undefined}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>
      <div className="mb-6">
        <LibraryCardControls
          settings={cardSettings}
          onChange={handleCardSettings}
          accent={accent}
          themeMode={props.themeMode}
          onOpenPreview={props.onOpenCardStylePreview}
        />
      </div>

      {props.items.length === 0 ? (
        <div className={`text-center py-16 ${isLight ? "text-slate-600" : "text-slate-500"}`}>No library items match the current filter.</div>
      ) : (
        <div className="flex items-start gap-4 mb-8">
          <div className="flex-1 space-y-8 overflow-visible">
            {groupedItems.letters.map((letter) => (
              <div
                key={letter}
                ref={(el) => {
                  sectionRefs.current[letter] = el;
                }}
              >
                <div
                  className={`mb-3 text-[14px] font-headline uppercase tracking-widest border-b pb-2 ${
                    isLight ? "text-slate-700 border-slate-200" : "text-slate-500 border-[#424753]/25"
                  }`}
                >
                  {letter}
                </div>
                <div className="overflow-visible">
                  {cardSettings.viewMode === "list" ? (
                    <div style={libraryListStyle()}>
                      {(groupedItems.groups[letter] || []).map((item) => (
                        <LibraryListRow
                          key={item.id}
                          item={item}
                          posterWidthPx={cardSettings.posterWidthPx}
                          accent={accent}
                          themeMode={props.themeMode}
                          onClick={() => props.onOpenDetail(item)}
                        />
                      ))}
                    </div>
                  ) : (
                    <div style={libraryPosterGridStyle(cardSettings.posterWidthPx)}>
                      {(groupedItems.groups[letter] || []).map((item) => (
                        <div
                          key={item.id}
                          className={libraryPosterGridItemClassName}
                          style={libraryPosterGridItemStyle()}
                        >
                          <LibraryGridCard
                            item={item}
                            variant={cardSettings.variant}
                            posterWidthPx={cardSettings.posterWidthPx}
                            accent={accent}
                            themeMode={props.themeMode}
                            onClick={() => props.onOpenDetail(item)}
                          />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div
            className={`hidden lg:flex sticky top-24 flex-col gap-1 rounded-lg border px-2 py-2 ${
              isLight ? "border-slate-200 bg-white/95 shadow-sm backdrop-blur-sm" : "border-[#424753]/35 bg-[#111722]/90"
            }`}
          >
            {groupedItems.letters.map((letter) => (
              <button
                key={`alpha-${letter}`}
                type="button"
                onClick={() => sectionRefs.current[letter]?.scrollIntoView({ behavior: "smooth", block: "start" })}
                className={`w-6 h-6 rounded text-[12px] font-headline font-bold transition-colors ${
                  isLight
                    ? "text-slate-600 hover:text-slate-900 hover:bg-slate-100"
                    : "text-slate-400 hover:text-white hover:bg-[#293346]"
                }`}
                title={`Jump to ${letter}`}
              >
                {letter}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-4">
        <div
          className={`rounded-xl border p-5 ${
            isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          <div className="flex justify-between items-start">
            <div className={`text-[12px] font-headline uppercase tracking-widest mb-3 ${isLight ? "text-slate-500" : "text-slate-400"}`}>Total Items</div>
            <span className={`material-symbols-outlined ${isLight ? "text-slate-400" : "text-slate-600"}`} style={{ fontSize: 18 }}>storage</span>
          </div>
          <div className={`text-[32px] font-black font-headline ${isLight ? "text-slate-900" : "text-white"}`}>{props.catalogTotal}</div>
        </div>
        <div
          className={`rounded-xl border p-5 ${
            isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          <div className="flex justify-between items-start">
            <div className={`text-[12px] font-headline uppercase tracking-widest mb-3 ${isLight ? "text-slate-500" : "text-slate-400"}`}>Missing Assets</div>
            <span className="material-symbols-outlined text-yellow-500" style={{ fontSize: 18 }}>warning</span>
          </div>
          <div className={`text-[32px] font-black font-headline ${isLight ? "text-slate-900" : "text-white"}`}>{totalMissing}</div>
          {totalMissing > 0 && (
            <button
              type="button"
              onClick={() => props.onFilterChange("missing")}
              className="mt-3 text-[14px] font-headline uppercase tracking-wider flex items-center gap-1"
              style={{ color: accent.icon }}
            >
              View Errors <span className="material-symbols-outlined" style={{ fontSize: 14 }}>arrow_forward</span>
            </button>
          )}
        </div>
        <div
          className={`rounded-xl border p-5 ${
            isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          <div className="flex justify-between items-start">
            <div className={`text-[12px] font-headline uppercase tracking-widest mb-3 ${isLight ? "text-slate-500" : "text-slate-400"}`}>Sync Status</div>
            <span className="material-symbols-outlined" style={{ fontSize: 18, color: accent.hex }}>sync</span>
          </div>
          <div className="flex items-center gap-2 mb-1">
            <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
            <span className={`font-bold font-headline text-[16px] ${isLight ? "text-slate-900" : "text-white"}`}>Active</span>
          </div>
          <div className={`text-[14px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>Library indexed</div>
        </div>
      </div>
    </div>
  );
}
