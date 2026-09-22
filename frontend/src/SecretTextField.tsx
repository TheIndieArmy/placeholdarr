import { useState, type ReactNode } from "react";

/** Secret settings input: masked by default with an explicit Show / Hide control. */
export function SecretTextField(props: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  inputClassName?: string;
  buttonClassName?: string;
  ariaLabel?: string;
  /** Extra controls after Show/Hide (e.g. Test). */
  trailing?: ReactNode;
}) {
  const [revealed, setRevealed] = useState(false);
  return (
    <div className={`flex gap-2 ${props.className ?? ""}`}>
      <input
        className={
          props.inputClassName ??
          "min-w-0 flex-1 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[16px] text-slate-200 placeholder-slate-600 outline-none transition-colors"
        }
        type={revealed ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        disabled={props.disabled}
        value={props.value}
        placeholder={props.placeholder}
        aria-label={props.ariaLabel}
        onChange={(e) => props.onChange(e.target.value)}
      />
      <button
        type="button"
        disabled={props.disabled}
        className={
          props.buttonClassName ??
          "inline-flex shrink-0 items-center justify-center rounded-lg border border-[#424753]/50 bg-[#252e3a]/80 px-3 text-[12px] font-headline uppercase tracking-wider text-slate-300 transition hover:border-[#424753]/80 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        }
        onClick={() => setRevealed((v) => !v)}
      >
        {revealed ? "Hide" : "Show"}
      </button>
      {props.trailing}
    </div>
  );
}
