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
  action?: ReactNode;
  valueNode?: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  const display = props.value?.trim() || "—";
  const valueClass = `min-w-0 break-words font-mono text-[13px] text-right ${isLight ? "text-slate-800" : "text-slate-200"}`;

  return (
    <div className="flex items-start justify-between gap-4">
      <span className="shrink-0 text-[12px] font-headline uppercase tracking-wider text-slate-500">
        {props.label}
      </span>
      <div className="flex items-start justify-end gap-2 min-w-0">
        {props.valueNode != null ? (
          props.valueNode
        ) : props.href ? (
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
        {props.action}
      </div>
    </div>
  );
}

export function DetailFactTagList(props: {
  tags?: string[] | null;
  themeMode: ThemeMode;
}) {
  const isLight = props.themeMode === "light";
  const tags = (props.tags || []).map((tag) => String(tag || "").trim()).filter(Boolean);
  if (!tags.length) {
    return <span className={`font-mono text-[13px] ${isLight ? "text-slate-800" : "text-slate-200"}`}>—</span>;
  }
  return (
    <div className="flex min-w-0 flex-wrap justify-end gap-1">
      {tags.map((tag) => (
        <span
          key={tag}
          className={`inline-flex max-w-full items-center rounded-md border px-1.5 py-0.5 text-[12px] font-mono leading-snug ${
            isLight
              ? "border-slate-200 bg-slate-50 text-slate-800"
              : "border-[#424753]/50 bg-[#1e2430] text-slate-200"
          }`}
          title={tag}
        >
          <span className="truncate">{tag}</span>
        </span>
      ))}
    </div>
  );
}
