export const TOGGLE_TRACK_OFF =
  "color-mix(in srgb, var(--brand-accent-tertiary) 38%, var(--brand-surface-elevated))";
export const TOGGLE_TRACK_OFF_BORDER =
  "color-mix(in srgb, var(--brand-accent-tertiary) 28%, var(--brand-border-subtle))";

/** Brand-consistent pill toggle — tertiary-tinted track when off, accent fill when on. */
export function ToggleSwitch(props: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  accentHex: string;
  disabled?: boolean;
  ariaLabel?: string;
  /** `sm` matches legacy w-9 toggles (ARR cards, table rows). */
  size?: "sm" | "md";
  className?: string;
}) {
  const { checked, onChange, accentHex, disabled, ariaLabel, size = "md", className } = props;
  const thumbShift = size === "sm" ? "translate-x-4" : "translate-x-5";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => {
        if (!disabled) onChange(!checked);
      }}
      className={`flex h-5 shrink-0 items-center rounded-full border px-0.5 transition-colors ${
        size === "sm" ? "w-9" : "w-10"
      } ${disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"} ${className ?? ""}`}
      style={
        checked
          ? { backgroundColor: accentHex, borderColor: accentHex }
          : { backgroundColor: TOGGLE_TRACK_OFF, borderColor: TOGGLE_TRACK_OFF_BORDER }
      }
    >
      <div
        className={`h-4 w-4 rounded-full bg-white shadow transition-transform ${
          checked ? thumbShift : "translate-x-0"
        }`}
      />
    </button>
  );
}
