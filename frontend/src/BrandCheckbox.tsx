import React from "react";

const SIZE_PX: Record<"sm" | "md", number> = {
  sm: 14,
  md: 16,
};

/** Accent-aware branded checkbox (button role=checkbox; not a native OS checkbox). */
export function BrandCheckbox(props: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  size?: "sm" | "md";
  accentHex?: string;
  align?: "start" | "center";
  ariaLabel?: string;
  className?: string;
}) {
  const size = props.size ?? "md";
  const px = SIZE_PX[size];
  const accent = props.accentHex || "var(--brand-accent)";

  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={props.checked}
      aria-label={props.ariaLabel}
      disabled={props.disabled}
      onClick={() => {
        if (!props.disabled) props.onChange(!props.checked);
      }}
      className={[
        "inline-flex shrink-0 items-center justify-center rounded border transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]",
        props.align === "start" ? "mt-0.5" : "",
        props.disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
        props.className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{
        width: px,
        height: px,
        borderColor: props.checked ? accent : "color-mix(in srgb, #424753 60%, transparent)",
        backgroundColor: props.checked ? accent : "#0f1419",
      }}
    >
      {props.checked ? (
        <span
          className="material-symbols-outlined text-white"
          style={{ fontSize: Math.max(12, px - 2), fontVariationSettings: "'FILL' 1" }}
          aria-hidden
        >
          check
        </span>
      ) : null}
    </button>
  );
}
