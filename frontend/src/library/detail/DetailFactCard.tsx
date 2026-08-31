import type { ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";

export function DetailFactCard(props: {
  title: string;
  themeMode: ThemeMode;
  children: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  return (
    <div
      className={`rounded-xl border p-4 ${isLight ? "bg-white border-[#d7e2f0] shadow-sm" : "bg-[#171c22] border-[#424753]/40"}`}
    >
      <div className="text-[11px] font-headline uppercase tracking-widest text-slate-500 mb-3">{props.title}</div>
      <div className="space-y-2 text-[14px]">{props.children}</div>
    </div>
  );
}

export function DetailFactRow(props: {
  label: string;
  value?: string | null;
  href?: string | null;
  themeMode: ThemeMode;
}) {
  const isLight = props.themeMode === "light";
  const display = props.value?.trim() || "—";
  const valueClass = `min-w-0 break-words font-mono text-[13px] text-right ${isLight ? "text-slate-800" : "text-slate-200"}`;

  return (
    <div className="flex items-start justify-between gap-4">
      <span className="shrink-0 text-[12px] font-headline uppercase tracking-wider text-slate-500">
        {props.label}
      </span>
      {props.href ? (
        <a
          href={props.href}
          target="_blank"
          rel="noreferrer"
          className={`${valueClass} hover:underline`}
        >
          {display}
        </a>
      ) : (
        <span className={valueClass}>{display}</span>
      )}
    </div>
  );
}
