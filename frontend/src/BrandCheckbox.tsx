import React from "react";
import type { ThemeMode } from "./brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS } from "./brandAccentUi";

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
  /** When set, checked fill/check colors follow brand pairing by theme. */
  themeMode?: ThemeMode;
  align?: "start" | "center";
  ariaLabel?: string;
  className?: string;
}) {
  const size = props.size ?? "md";
  const px = SIZE_PX[size];
  const accent = props.accentHex || "var(--brand-accent)";
  const isLight = props.themeMode === "light";
  // Dark: yellow box + dark-blue check. Light: dark-blue box + yellow check.
  const checkedFill = isLight ? "var(--brand-fg-on-accent)" : accent;
  const checkedBorder = checkedFill;
  const uncheckedBg = isLight ? "#ffffff" : "#0f1419";
  const uncheckedBorder = isLight
    ? "color-mix(in srgb, #94a3b8 70%, transparent)"
    : "color-mix(in srgb, #424753 60%, transparent)";

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
        borderColor: props.checked ? checkedBorder : uncheckedBorder,
        backgroundColor: props.checked ? checkedFill : uncheckedBg,
      }}
    >
      {props.checked ? (
        <span
          className={
            isLight
              ? "material-symbols-outlined leading-none"
              : `material-symbols-outlined leading-none ${FG_ON_ACCENT_TEXT_CLASS}`
          }
          style={{
            fontSize: Math.max(12, px - 2),
            fontVariationSettings: "'FILL' 1",
            ...(isLight ? { color: accent } : null),
          }}
          aria-hidden
        >
          check
        </span>
      ) : null}
    </button>
  );
}
