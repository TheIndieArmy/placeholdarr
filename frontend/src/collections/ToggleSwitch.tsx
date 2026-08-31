import { ToggleSwitch } from "../ToggleSwitch";

export { ToggleSwitch };

/** AND/OR operator control — switch slides toward the active operator. */
export function AndOrToggle(props: {
  op: "and" | "or";
  onChange: (op: "and" | "or") => void;
  accentHex: string;
  mutedClass: string;
  disabled?: boolean;
}) {
  const isOr = props.op === "or";
  const active = "text-[11px] font-headline font-semibold uppercase tracking-wider";
  const inactive = "text-[11px] font-headline uppercase tracking-wider opacity-45";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className={isOr ? inactive : active} style={isOr ? undefined : { color: props.accentHex }}>
        AND
      </span>
      <ToggleSwitch
        checked={isOr}
        onChange={(checked) => props.onChange(checked ? "or" : "and")}
        accentHex={props.accentHex}
        disabled={props.disabled}
        ariaLabel={isOr ? "Switch to AND" : "Switch to OR"}
      />
      <span className={isOr ? active : inactive} style={isOr ? { color: props.accentHex } : undefined}>
        OR
      </span>
      <span className={props.mutedClass}>{isOr ? "Any can match" : "All must match"}</span>
    </div>
  );
}
