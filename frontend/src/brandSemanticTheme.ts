import type { Brand, ThemeMode } from "./brandTypes";

/**
 * Simularr **Spectral Data** marketing headline / rail cyan (style guide art).
 * Simularr dark UI ice blue is separate: token `accentIce` `#7DD3FC`.
 */
export const SIMULARR_SPECTRAL_CYAN_HEX = "#22D3EE";

export type BrandSemanticTokens = {
  fontHeadline: string;
  fontBody: string;
  fontLabel: string;
  fontMono: string;
  fg: string;
  fgMuted: string;
  fgSubtle: string;
  fgLabel: string;
  fgOnAccent: string;
  surfacePanel: string;
  surfaceElevated: string;
  surfaceMuted: string;
  surfaceInput: string;
  border: string;
  borderStrong: string;
  borderSubtle: string;
  glassBg: string;
  glassBorder: string;
  accent: string;
  accent2: string;
  accent3: string;
  accentIce: string;
  danger: string;
  success: string;
  /** Page / chrome (light mode solids or dark glass tints) */
  chromePage: string;
  chromeSidebar: string;
  chromeHeader: string;
  chromeMain: string;
  /** Light mode: inactive nav row hover */
  navHover: string;
  /** Unified top strip (sidebar brand row + main header), distinct from sidebar chrome */
  topBarBand: string;
};

type Accent = { hex: string; text: string; icon: string; hoverHex: string };

function defaultsFromAccent(accent: Accent, mode: ThemeMode): BrandSemanticTokens {
  if (mode === "light") {
    return {
      fontHeadline: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `Inter, ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#0f172a",
      fgMuted: "#475569",
      fgSubtle: "#64748b",
      fgLabel: accent.hoverHex,
      fgOnAccent: "#0f172a",
      surfacePanel: "#ffffff",
      surfaceElevated: "#f1f5f9",
      surfaceMuted: "#e2e8f0",
      surfaceInput: "#ffffff",
      border: "#cbd5e1",
      borderStrong: "#94a3b8",
      borderSubtle: "rgba(148, 163, 184, 0.45)",
      glassBg: "rgba(255,255,255,0.92)",
      glassBorder: "rgba(148, 163, 184, 0.5)",
      accent: accent.hex,
      accent2: accent.icon,
      accent3: accent.hoverHex,
      accentIce: "#38bdf8",
      danger: "#dc2626",
      success: "#16a34a",
      chromePage: "#e3e3e5",
      chromeSidebar: "#e6e6e8",
      chromeHeader: "#ececee",
      chromeMain: "#eef3f8",
      navHover: "#e4edf8",
      topBarBand: "#e2e8f0",
    };
  }
  return {
    fontHeadline: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
    fontBody: `Inter, ui-sans-serif, system-ui, sans-serif`,
    fontLabel: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
    fontMono: `"JetBrains Mono", ui-monospace, monospace`,
    fg: "#e2e8f0",
    fgMuted: "#94a3b8",
    fgSubtle: "#64748b",
    fgLabel: accent.icon,
    fgOnAccent: "#0b1020",
    surfacePanel: "#171c22",
    surfaceElevated: "#1e2430",
    surfaceMuted: "#252e3a",
    surfaceInput: "#1a2332",
    border: "#334155",
    borderStrong: "#475569",
    borderSubtle: "rgba(66, 71, 83, 0.45)",
    glassBg: "rgba(255,255,255,0.06)",
    glassBorder: "rgba(255,255,255,0.12)",
    accent: accent.hex,
    accent2: accent.icon,
    accent3: accent.hoverHex,
    accentIce: "#7dd3fc",
    danger: "#f87171",
    success: "#4ade80",
    chromePage: "#0b1320",
    chromeSidebar: "#121a24",
    chromeHeader: "#141c28",
    chromeMain: "#0f1419",
    navHover: "rgba(255,255,255,0.06)",
    topBarBand: "#181f2a",
  };
}

export function getBrandSemanticTokens(brand: Brand, mode: ThemeMode, accent: Accent): BrandSemanticTokens {
  if (brand === "simularr" && mode === "dark") {
    return {
      fontHeadline: `"Sora", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `Inter, ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"JetBrains Mono", ui-monospace, monospace`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#E2E8F0",
      fgMuted: "#94A3B8",
      fgSubtle: "#64748B",
      fgLabel: "#7DD3FF",
      fgOnAccent: "#0F172A",
      surfacePanel: "#0F172A",
      surfaceElevated: "#1E293B",
      surfaceMuted: "#0B1326",
      surfaceInput: "#0B1326",
      border: "#334155",
      borderStrong: "#475569",
      borderSubtle: "rgba(52, 218, 255, 0.22)",
      glassBg: "rgba(15, 23, 42, 0.55)",
      glassBorder: "rgba(52, 218, 255, 0.22)",
      accent: "#FBBF24",
      accent2: "#FDE047",
      accent3: "#34DAFF",
      accentIce: "#7DD3FC",
      danger: "#FCA5A5",
      success: "#86EFAC",
      chromePage: "#0B1326",
      chromeSidebar: "#0F172A",
      chromeHeader: "#0F172A",
      chromeMain: "#0B1326",
      navHover: "rgba(52, 218, 255, 0.12)",
      topBarBand: "#FBBF24",
    };
  }
  if (brand === "simularr" && mode === "light") {
    return {
      fontHeadline: `"Sora", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `Inter, ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"JetBrains Mono", ui-monospace, monospace`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#0F172A",
      fgMuted: "#475569",
      fgSubtle: "#64748B",
      fgLabel: "#B45309",
      fgOnAccent: "#0F172A",
      surfacePanel: "#FFFFFF",
      surfaceElevated: "#F1F5F9",
      surfaceMuted: "#E2E8F0",
      surfaceInput: "#F8FAFC",
      border: "#94A3B8",
      borderStrong: "#64748B",
      borderSubtle: "rgba(148, 163, 184, 0.4)",
      glassBg: "rgba(255,255,255,0.95)",
      glassBorder: "rgba(52, 218, 255, 0.28)",
      accent: "#CA8A04",
      accent2: "#0EA5E9",
      accent3: "#34DAFF",
      accentIce: "#0284C7",
      danger: "#B91C1C",
      success: "#15803D",
      chromePage: "#E8EEF5",
      chromeSidebar: "#DCE6F4",
      chromeHeader: "#F1F5F9",
      chromeMain: "#F8FAFC",
      navHover: "#CBD5E1",
      topBarBand: "#FDE047",
    };
  }
  if (brand === "trigger" && mode === "light") {
    return {
      fontHeadline: `"Archivo", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"Archivo", ui-sans-serif, system-ui, sans-serif`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#212121",
      fgMuted: "#744F10",
      fgSubtle: "#92400E",
      fgLabel: "#FF6B35",
      fgOnAccent: "#212121",
      surfacePanel: "#FFFCF8",
      surfaceElevated: "#FCEEE3",
      surfaceMuted: "#FAF3ED",
      surfaceInput: "#FFFFFF",
      border: "#E7D8C8",
      borderStrong: "#D4B89A",
      borderSubtle: "rgba(116, 79, 16, 0.2)",
      glassBg: "rgba(255, 252, 248, 0.96)",
      glassBorder: "rgba(255, 107, 53, 0.28)",
      accent: "#FF6B35",
      accent2: "#FFCC33",
      accent3: "#744F10",
      accentIce: "#F97316",
      danger: "#B91C1C",
      success: "#166534",
      chromePage: "#FAF3ED",
      chromeSidebar: "#F5EBE0",
      chromeHeader: "#FFFCF8",
      chromeMain: "#FFFDFB",
      navHover: "#FCEEE3",
      topBarBand: "#f2e4d8",
    };
  }
  if (brand === "trigger" && mode === "dark") {
    return {
      fontHeadline: `"Archivo", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"JetBrains Mono", ui-monospace, monospace`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#FCEEE3",
      fgMuted: "#D6C4B4",
      fgSubtle: "#A89B8F",
      fgLabel: "#FFCC33",
      fgOnAccent: "#212121",
      surfacePanel: "#2A2A2A",
      surfaceElevated: "#333333",
      surfaceMuted: "#1A1A1A",
      surfaceInput: "#262626",
      border: "#404040",
      borderStrong: "#525252",
      borderSubtle: "rgba(255, 204, 51, 0.15)",
      glassBg: "rgba(33, 33, 33, 0.72)",
      glassBorder: "rgba(255, 107, 53, 0.22)",
      accent: "#FF6B35",
      accent2: "#FFCC33",
      accent3: "#F97316",
      accentIce: "#FDBA74",
      danger: "#FCA5A5",
      success: "#86EFAC",
      chromePage: "#212121",
      chromeSidebar: "#1C1C1C",
      chromeHeader: "#242424",
      chromeMain: "#212121",
      navHover: "rgba(255,255,255,0.06)",
      topBarBand: "#1a1a1a",
    };
  }
  if (brand === "linkarr" && mode === "light") {
    return {
      fontHeadline: `"Outfit", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `Inter, ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"Space Grotesk", ui-sans-serif, system-ui, sans-serif`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#1E293B",
      fgMuted: "#475569",
      fgSubtle: "#64748B",
      fgLabel: "#2563EB",
      fgOnAccent: "#FFFFFF",
      surfacePanel: "#FFFFFF",
      surfaceElevated: "#F1F5F9",
      surfaceMuted: "#E0F2FE",
      surfaceInput: "#FFFFFF",
      border: "#BFDBFE",
      borderStrong: "#93C5FD",
      borderSubtle: "rgba(37, 99, 235, 0.12)",
      glassBg: "rgba(255,255,255,0.94)",
      glassBorder: "rgba(96, 165, 250, 0.35)",
      accent: "#2563EB",
      accent2: "#60A5FA",
      accent3: "#1D4ED8",
      accentIce: "#38BDF8",
      danger: "#DC2626",
      success: "#16A34A",
      chromePage: "#EFF6FF",
      chromeSidebar: "#E0F0FF",
      chromeHeader: "#FFFFFF",
      chromeMain: "#F8FBFF",
      navHover: "#DBEAFE",
      topBarBand: "#dbeafe",
    };
  }
  if (brand === "linkarr" && mode === "dark") {
    return {
      fontHeadline: `"Outfit", ui-sans-serif, system-ui, sans-serif`,
      fontBody: `Inter, ui-sans-serif, system-ui, sans-serif`,
      fontLabel: `"JetBrains Mono", ui-monospace, monospace`,
      fontMono: `"JetBrains Mono", ui-monospace, monospace`,
      fg: "#E2E8F0",
      fgMuted: "#94A3B8",
      fgSubtle: "#64748B",
      fgLabel: "#60A5FA",
      fgOnAccent: "#F8FAFC",
      surfacePanel: "#1E293B",
      surfaceElevated: "#27364F",
      surfaceMuted: "#0F172A",
      surfaceInput: "#162032",
      border: "#334155",
      borderStrong: "#475569",
      borderSubtle: "rgba(96, 165, 250, 0.2)",
      glassBg: "rgba(30, 41, 59, 0.55)",
      glassBorder: "rgba(96, 165, 250, 0.2)",
      accent: "#2563EB",
      accent2: "#60A5FA",
      accent3: "#38BDF8",
      accentIce: "#93C5FD",
      danger: "#FCA5A5",
      success: "#4ADE80",
      chromePage: "#0F172A",
      chromeSidebar: "#1E293B",
      chromeHeader: "#1E293B",
      chromeMain: "#0F172A",
      navHover: "rgba(255,255,255,0.06)",
      topBarBand: "#172554",
    };
  }
  return defaultsFromAccent(accent, mode);
}

export function semanticTokensToCssVars(t: BrandSemanticTokens): Record<string, string> {
  return {
    "--brand-font-headline": t.fontHeadline,
    "--brand-font-body": t.fontBody,
    "--brand-font-label": t.fontLabel,
    "--brand-font-mono": t.fontMono,
    "--brand-fg": t.fg,
    "--brand-fg-muted": t.fgMuted,
    "--brand-fg-subtle": t.fgSubtle,
    "--brand-fg-label": t.fgLabel,
    "--brand-fg-on-accent": t.fgOnAccent,
    "--brand-surface-panel": t.surfacePanel,
    "--brand-surface-elevated": t.surfaceElevated,
    "--brand-surface-muted": t.surfaceMuted,
    "--brand-surface-input": t.surfaceInput,
    "--brand-border": t.border,
    "--brand-border-strong": t.borderStrong,
    "--brand-border-subtle": t.borderSubtle,
    "--brand-glass-bg": t.glassBg,
    "--brand-glass-border": t.glassBorder,
    "--brand-accent": t.accent,
    "--brand-accent-2": t.accent2,
    "--brand-accent-3": t.accent3,
    "--brand-accent-tertiary": t.accent3,
    "--brand-accent-ice": t.accentIce,
    "--brand-danger": t.danger,
    "--brand-success": t.success,
    "--brand-chrome-page": t.chromePage,
    "--brand-chrome-sidebar": t.chromeSidebar,
    "--brand-chrome-header": t.chromeHeader,
    "--brand-chrome-main": t.chromeMain,
    "--brand-nav-hover": t.navHover,
    "--brand-top-bar-band": t.topBarBand,
  };
}
