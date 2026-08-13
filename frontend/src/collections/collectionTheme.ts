import { createContext, useContext } from "react";

/** Tailwind class bundles for Collections UI — light and dark variants. */
export type CollectionTheme = {
  field: string;
  /** Native select — extra right padding so the chevron does not overlap option text. */
  selectField: string;
  /** Filter/source operation dropdowns with longer labels (e.g. "released in the past"). */
  selectOp: string;
  identityCard: string;
  blockCard: string;
  blockHeader: string;
  blockTitle: string;
  blockSubtitle: string;
  previewRail: string;
  previewHeader: string;
  dropdown: string;
  dropdownItem: string;
  dropdownItemTitle: string;
  dropdownItemDescription: string;
  dropdownEmpty: string;
  label: string;
  muted: string;
  sectionLabel: string;
  heading: string;
  connectorLine: string;
  connectorPill: string;
  dashedButton: string;
  dashedPanel: string;
  chipInactive: string;
  chipShowMore: string;
  pinTag: string;
  pinTitle: string;
  pinYear: string;
  previewStage: string;
  previewValue: string;
  divider: string;
  cancelButton: string;
  iconMuted: string;
  iconAction: string;
  posterFallback: string;
  stageLine: string;
  stageLineInactive: string;
  explainStage: string;
  explainSkip: string;
  explainCheck: string;
  sampleFallback: string;
};

export function getCollectionTheme(isLight: boolean): CollectionTheme {
  if (isLight) {
    return {
      field:
        "bg-white border border-[#cddbeb] rounded-md px-2.5 py-1.5 text-[14px] text-slate-900 outline-none focus:border-sky-400/80 placeholder:text-slate-400",
      selectField:
        "bg-white border border-[#cddbeb] rounded-md pl-2.5 pr-8 py-1.5 text-[14px] text-slate-900 outline-none focus:border-sky-400/80 min-w-[8rem] shrink-0",
      selectOp:
        "bg-white border border-[#cddbeb] rounded-md pl-2.5 pr-9 py-1.5 text-[14px] text-slate-900 outline-none focus:border-sky-400/80 min-w-[12.5rem] shrink-0",
      identityCard: "rounded-xl border border-slate-200/90 bg-white shadow-sm px-4 py-3.5 mb-4",
      blockCard: "rounded-xl border border-slate-200/90 bg-white shadow-sm",
      blockHeader: "flex items-center gap-2.5 px-4 py-2.5 border-b border-slate-200/80 bg-[#f2f7ff]",
      blockTitle: "text-[14px] font-headline uppercase tracking-wider text-slate-900",
      blockSubtitle: "text-[12px] text-slate-500 truncate",
      previewRail: "rounded-xl border border-slate-200/90 bg-white shadow-sm overflow-hidden lg:sticky lg:top-4 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto",
      previewHeader: "flex items-center gap-2 px-4 py-3 border-b border-slate-200/80 bg-[#f2f7ff]",
      dropdown: "absolute z-20 mt-1 w-full overflow-hidden rounded-xl border border-slate-200/90 bg-white shadow-lg",
      dropdownItem: "flex w-full text-left hover:bg-[#f2f7ff] transition-colors disabled:opacity-40 disabled:cursor-not-allowed",
      dropdownItemTitle: "block text-[14px] text-slate-900",
      dropdownItemDescription: "block text-[12px] text-slate-500",
      dropdownEmpty: "absolute z-20 mt-1 w-full rounded-xl border border-slate-200/90 bg-white px-3 py-2 text-[13px] text-slate-500 shadow-lg",
      label: "text-[13px] text-slate-600",
      muted: "text-[13px] text-slate-500",
      sectionLabel: "text-[12px] font-headline uppercase tracking-widest text-slate-500",
      heading: "text-[14px] font-headline uppercase tracking-wider text-slate-900",
      connectorLine: "bg-slate-300/70",
      connectorPill: "rounded-full border border-slate-200 bg-white px-3 py-0.5 text-[11px] font-headline uppercase tracking-widest shadow-sm",
      dashedButton:
        "flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-slate-300 px-4 py-2.5 text-[13px] font-headline uppercase tracking-wider text-slate-500 hover:text-slate-800 hover:border-slate-400 transition-colors",
      dashedPanel: "rounded-xl border border-dashed border-slate-200 px-4 py-3 text-[13px] text-slate-500",
      chipInactive: "text-slate-600 border-slate-200 hover:border-slate-300",
      chipShowMore: "text-slate-500 border-dashed border-slate-200 hover:text-slate-700",
      pinTag: "inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-[#f8fafc] pl-1 pr-1.5 py-1",
      pinTitle: "text-[13px] text-slate-800",
      pinYear: "text-slate-500",
      previewStage: "flex-1 text-[13px] text-slate-600",
      previewValue: "text-[15px] font-mono text-slate-900",
      divider: "border-slate-200/80",
      cancelButton:
        "rounded-lg border border-slate-200 px-5 py-2 text-[14px] font-headline uppercase tracking-wider text-slate-600 hover:text-slate-900 hover:bg-slate-50 transition-colors",
      iconMuted: "text-slate-500",
      iconAction: "text-slate-500 hover:text-slate-800",
      posterFallback: "border border-slate-200 bg-[#f2f7ff] text-slate-400",
      stageLine: "bg-slate-300/60",
      stageLineInactive: "bg-slate-200",
      explainStage: "text-slate-700",
      explainSkip: "text-slate-400",
      explainCheck: "text-slate-500",
      sampleFallback: "border border-slate-200 bg-[#f2f7ff] text-slate-500",
    };
  }

  return {
    field:
      "bg-[#11161d] border border-[#424753]/50 rounded-md px-2.5 py-1.5 text-[14px] text-slate-200 outline-none focus:border-slate-400/60 placeholder:text-slate-500",
    selectField:
      "bg-[#11161d] border border-[#424753]/50 rounded-md pl-2.5 pr-8 py-1.5 text-[14px] text-slate-200 outline-none focus:border-slate-400/60 min-w-[8rem] shrink-0",
    selectOp:
      "bg-[#11161d] border border-[#424753]/50 rounded-md pl-2.5 pr-9 py-1.5 text-[14px] text-slate-200 outline-none focus:border-slate-400/60 min-w-[12.5rem] shrink-0",
    identityCard: "rounded-xl border border-[#424753]/50 bg-[#171c22] px-4 py-3.5 mb-4",
    blockCard: "rounded-xl border border-[#424753]/50 bg-[#1a212b]",
    blockHeader: "flex items-center gap-2.5 px-4 py-2.5 border-b border-[#424753]/30 bg-[#1e2430]",
    blockTitle: "text-[14px] font-headline uppercase tracking-wider text-slate-200",
    blockSubtitle: "text-[12px] text-slate-500 truncate",
    previewRail:
      "rounded-xl border border-[#424753]/50 bg-[#171c22] overflow-hidden lg:sticky lg:top-4 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto",
    previewHeader: "flex items-center gap-2 px-4 py-3 border-b border-[#424753]/30 bg-[#1e2430]",
    dropdown: "absolute z-20 mt-1 w-full overflow-hidden rounded-xl border border-[#424753]/60 bg-[#11161d] shadow-2xl",
    dropdownItem: "flex w-full text-left hover:bg-[#1e2430] transition-colors disabled:opacity-40 disabled:cursor-not-allowed",
    dropdownItemTitle: "block text-[14px] text-slate-200",
    dropdownItemDescription: "block text-[12px] text-slate-500",
    dropdownEmpty:
      "absolute z-20 mt-1 w-full rounded-xl border border-[#424753]/60 bg-[#11161d] px-3 py-2 text-[13px] text-slate-500 shadow-2xl",
    label: "text-[13px] text-slate-400",
    muted: "text-[13px] text-slate-500",
    sectionLabel: "text-[12px] font-headline uppercase tracking-widest text-slate-500",
    heading: "text-[14px] font-headline uppercase tracking-wider text-slate-200",
    connectorLine: "bg-[#424753]/60",
    connectorPill:
      "rounded-full border border-[#424753]/50 bg-[#11161d] px-3 py-0.5 text-[11px] font-headline uppercase tracking-widest",
    dashedButton:
      "flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-[#424753]/60 px-4 py-2.5 text-[13px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200 hover:border-slate-400/60 transition-colors",
    dashedPanel: "rounded-xl border border-dashed border-[#424753]/40 px-4 py-3 text-[13px] text-slate-500",
    chipInactive: "text-slate-300 border-[#424753]/60 hover:border-slate-400/70",
    chipShowMore: "text-slate-400 border-dashed border-[#424753]/60 hover:text-slate-200",
    pinTag: "inline-flex items-center gap-1.5 rounded-lg border border-[#424753]/50 bg-[#11161d] pl-1 pr-1.5 py-1",
    pinTitle: "text-[13px] text-slate-300",
    pinYear: "text-slate-500",
    previewStage: "flex-1 text-[13px] text-slate-400",
    previewValue: "text-[15px] font-mono text-slate-200",
    divider: "border-[#424753]/30",
    cancelButton:
      "rounded-lg border border-[#424753]/60 px-5 py-2 text-[14px] font-headline uppercase tracking-wider text-slate-300 hover:text-white transition-colors",
    iconMuted: "text-slate-500",
    iconAction: "text-slate-500 hover:text-red-400",
    posterFallback: "border border-[#424753]/40 bg-[#1e2430] text-slate-600",
    stageLine: "bg-[#424753]/50",
    stageLineInactive: "bg-[#424753]",
    explainStage: "text-slate-300",
    explainSkip: "text-slate-600",
    explainCheck: "text-slate-400",
    sampleFallback: "border border-[#424753]/40 bg-[#1e2430] text-slate-500",
  };
}

const CollectionThemeContext = createContext<CollectionTheme>(getCollectionTheme(false));

export const CollectionThemeProvider = CollectionThemeContext.Provider;

export function useCollectionTheme(): CollectionTheme {
  return useContext(CollectionThemeContext);
}
