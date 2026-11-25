import type { ReactNode } from "react";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";

export function StickyActionBar(props: {
  accentHex: string;
  issues?: string[];
  hint?: string;
  readyLabel?: string;
  primaryLabel: string;
  primaryBusyLabel?: string;
  primaryDisabled?: boolean;
  primaryLoading?: boolean;
  onPrimary: () => void;
  onCancel?: () => void;
  cancelLabel?: string;
  extra?: ReactNode;
}) {
  const issues = props.issues ?? [];
  return (
    <>
      <span className="mr-auto min-w-0 truncate text-[13px]" title={issues.join("\n")}>
        {props.hint && !props.issues ? (
          <span className="text-slate-400">{props.hint}</span>
        ) : issues.length ? (
          <span className="text-amber-300">
            {issues.length === 1 ? issues[0] : `${issues.length} things left: ${issues.join(", ")}`}
          </span>
        ) : (
          <span className="text-emerald-400">{props.readyLabel ?? "Ready to save"}</span>
        )}
      </span>
      {props.extra}
      {props.onCancel ? (
        <button
          type="button"
          onClick={props.onCancel}
          className="rounded-lg px-4 py-2 text-[13px] font-headline uppercase tracking-wider text-slate-300 hover:text-white"
        >
          {props.cancelLabel ?? "Cancel"}
        </button>
      ) : null}
      <button
        type="button"
        disabled={props.primaryDisabled || props.primaryLoading}
        onClick={props.onPrimary}
        className={`rounded-lg px-5 py-2 text-[13px] font-headline uppercase tracking-wider disabled:opacity-40 ${FG_ON_ACCENT_TEXT_CLASS}`}
        style={accentFilledStyle(props.accentHex)}
      >
        {props.primaryLoading ? (props.primaryBusyLabel ?? "Saving…") : props.primaryLabel}
      </button>
    </>
  );
}