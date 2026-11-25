import { useState, type ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";
import { getCollectionTheme } from "../collectionTheme";

export function BuilderSection(props: {
  number: number;
  title: string;
  summary?: string;
  /** Live count for what this section does (e.g. "120 → 34"), shown open or closed. */
  metric?: ReactNode;
  defaultOpen?: boolean;
  themeMode: ThemeMode;
  children: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const [open, setOpen] = useState(props.defaultOpen ?? true);

  return (
    <section
      className={`rounded-xl border ${isLight ? "border-slate-200 bg-white" : "border-[#424753]/50 bg-[#171c22]/80"}`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-3 px-4 py-3 text-left"
      >
        <span
          className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[12px] font-headline font-bold ${
            isLight ? "bg-slate-100 text-slate-800" : "bg-[#1e2430] text-slate-200"
          }`}
        >
          {props.number}
        </span>
        <span className="min-w-0 flex-1">
          <span className={`block text-[13px] font-headline uppercase tracking-wider ${theme.sectionLabel}`}>
            {props.title}
          </span>
          {!open && props.summary ? (
            <span className={`mt-1 block text-[13px] ${theme.muted}`}>{props.summary}</span>
          ) : null}
        </span>
        {props.metric ? (
          <span
            className={`mt-0.5 max-w-[18rem] shrink-0 truncate rounded-full px-2.5 py-0.5 font-mono text-[12px] ${
              isLight ? "bg-slate-100 text-slate-700" : "bg-[#1e2430] text-slate-200"
            }`}
          >
            {props.metric}
          </span>
        ) : null}
        <span className={`material-symbols-outlined shrink-0 ${theme.iconMuted}`} style={{ fontSize: 20 }}>
          {open ? "expand_less" : "expand_more"}
        </span>
      </button>
      {open ? <div className="border-t border-[#424753]/25 px-4 py-4">{props.children}</div> : null}
    </section>
  );
}