import type { ThemeMode } from "../../brandTypes";

export type PlaceholderPolicy = "auto" | "never" | "pinned";

export function PinGlyph(props: {
  policy: PlaceholderPolicy;
  size?: number;
  accentHex: string;
  themeMode: ThemeMode;
}) {
  const size = props.size ?? 18;
  const isLight = props.themeMode === "light";
  const muted = isLight ? "#64748b" : "#94a3b8";
  const danger = isLight ? "#b91c1c" : "#f87171";
  const stroke =
    props.policy === "never" ? danger : props.policy === "pinned" ? props.accentHex : muted;
  const fill = props.policy === "auto" ? "none" : stroke;

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <path
        d="M12 2c1.8 0 3.2 1.4 3.2 3.1 0 .7-.2 1.3-.6 1.8l1.7 1.7c.8.8.8 2 0 2.8l-.7.7-2.1-2.1-.4.4V19l-1.1 3-1.1-3v-8.6l-.4-.4-2.1 2.1-.7-.7c-.8-.8-.8-2 0-2.8l1.7-1.7c-.4-.5-.6-1.1-.6-1.8C8.8 3.4 10.2 2 12 2z"
        fill={fill}
        stroke={stroke}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {props.policy === "never" ? (
        <line x1="4" y1="4" x2="20" y2="20" stroke={danger} strokeWidth="2" />
      ) : null}
    </svg>
  );
}
