/** Client-side share format for poster layout designer profiles. */

export const LAYOUT_SHARE_FORMAT = "placeholdarr.poster_layouts";
export const LAYOUT_SHARE_FORMAT_VERSION = 1;
/** Must match services/poster_layouts.LAYOUT_DOC_VERSION. */
export const LAYOUT_DOC_VERSION_SUPPORTED = 1;

export type LayoutShareScope = "all" | "single";

export type LayoutShareProfile = {
  labels: unknown[];
  grayscale: boolean;
};

export type LayoutSharePayload = {
  format: typeof LAYOUT_SHARE_FORMAT;
  format_version: number;
  layout_doc_version: number;
  exported_at: string;
  scope: LayoutShareScope;
  /** Present when scope is single (and may be set for all). */
  profile_key?: string;
  profiles: Record<string, LayoutShareProfile>;
};

const MEDIA = ["movie", "tv", "episode"] as const;
const AVAILABILITY = ["placeholder", "real"] as const;
const VARIANTS = ["default", "coming_soon"] as const;

export const KNOWN_LAYOUT_PROFILE_KEYS: string[] = MEDIA.flatMap((kind) =>
  AVAILABILITY.flatMap((availability) =>
    VARIANTS.map((variant) => `${kind}.${availability}.${variant}`),
  ),
);

const KNOWN_KEY_SET = new Set(KNOWN_LAYOUT_PROFILE_KEYS);

export function isKnownLayoutProfileKey(key: string): boolean {
  return KNOWN_KEY_SET.has(key);
}

export function parseLayoutProfileKey(
  key: string,
): { kind: (typeof MEDIA)[number]; availability: (typeof AVAILABILITY)[number]; variant: (typeof VARIANTS)[number] } | null {
  const parts = String(key || "").split(".");
  if (parts.length !== 3) return null;
  const [kind, availability, variant] = parts;
  if (!(MEDIA as readonly string[]).includes(kind)) return null;
  if (!(AVAILABILITY as readonly string[]).includes(availability)) return null;
  if (!(VARIANTS as readonly string[]).includes(variant)) return null;
  return {
    kind: kind as (typeof MEDIA)[number],
    availability: availability as (typeof AVAILABILITY)[number],
    variant: variant as (typeof VARIANTS)[number],
  };
}

function asProfile(raw: unknown): LayoutShareProfile | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const labels = Array.isArray(obj.labels) ? obj.labels : [];
  return { labels, grayscale: Boolean(obj.grayscale) };
}

export function buildLayoutExportPayload(args: {
  scope: LayoutShareScope;
  draft: Record<string, { labels: unknown[]; grayscale?: boolean }>;
  activeKey: string;
  layoutDocVersion?: number;
}): LayoutSharePayload {
  const layoutDocVersion = args.layoutDocVersion ?? LAYOUT_DOC_VERSION_SUPPORTED;
  const exported_at = new Date().toISOString();
  if (args.scope === "single") {
    const source = args.draft[args.activeKey] || { labels: [], grayscale: false };
    return {
      format: LAYOUT_SHARE_FORMAT,
      format_version: LAYOUT_SHARE_FORMAT_VERSION,
      layout_doc_version: layoutDocVersion,
      exported_at,
      scope: "single",
      profile_key: args.activeKey,
      profiles: {
        [args.activeKey]: {
          labels: Array.isArray(source.labels) ? source.labels : [],
          grayscale: Boolean(source.grayscale),
        },
      },
    };
  }
  const profiles: Record<string, LayoutShareProfile> = {};
  for (const key of KNOWN_LAYOUT_PROFILE_KEYS) {
    const source = args.draft[key];
    if (!source) continue;
    profiles[key] = {
      labels: Array.isArray(source.labels) ? source.labels : [],
      grayscale: Boolean(source.grayscale),
    };
  }
  // Include any extra draft keys that are still known (already covered) or skip unknowns.
  return {
    format: LAYOUT_SHARE_FORMAT,
    format_version: LAYOUT_SHARE_FORMAT_VERSION,
    layout_doc_version: layoutDocVersion,
    exported_at,
    scope: "all",
    profiles,
  };
}

export function parseLayoutImportPayload(raw: unknown): LayoutSharePayload {
  if (!raw || typeof raw !== "object") {
    throw new Error("Invalid layout file: expected a JSON object.");
  }
  const obj = raw as Record<string, unknown>;
  if (obj.format !== LAYOUT_SHARE_FORMAT) {
    throw new Error('Invalid layout file: missing format "placeholdarr.poster_layouts".');
  }
  const formatVersion = Number(obj.format_version);
  if (!Number.isFinite(formatVersion) || formatVersion < 1) {
    throw new Error("Invalid layout file: unsupported format_version.");
  }
  if (formatVersion > LAYOUT_SHARE_FORMAT_VERSION) {
    throw new Error(
      `This layout file uses format_version ${formatVersion}; this app supports up to ${LAYOUT_SHARE_FORMAT_VERSION}.`,
    );
  }
  const layoutDocVersion = Number(obj.layout_doc_version);
  if (!Number.isFinite(layoutDocVersion) || layoutDocVersion < 1) {
    throw new Error("Invalid layout file: unsupported layout_doc_version.");
  }
  if (layoutDocVersion > LAYOUT_DOC_VERSION_SUPPORTED) {
    throw new Error(
      `This layout file requires layout_doc_version ${layoutDocVersion}; this app supports up to ${LAYOUT_DOC_VERSION_SUPPORTED}.`,
    );
  }
  const scopeRaw = String(obj.scope || "").trim().toLowerCase();
  const scope: LayoutShareScope = scopeRaw === "single" ? "single" : scopeRaw === "all" ? "all" : "all";
  if (scopeRaw && scopeRaw !== "single" && scopeRaw !== "all") {
    throw new Error('Invalid layout file: scope must be "all" or "single".');
  }

  const profilesIn = obj.profiles;
  if (!profilesIn || typeof profilesIn !== "object" || Array.isArray(profilesIn)) {
    throw new Error("Invalid layout file: profiles must be an object.");
  }

  const profiles: Record<string, LayoutShareProfile> = {};
  for (const [key, value] of Object.entries(profilesIn as Record<string, unknown>)) {
    if (!isKnownLayoutProfileKey(key)) continue;
    const profile = asProfile(value);
    if (!profile) continue;
    profiles[key] = profile;
  }

  if (Object.keys(profiles).length === 0) {
    throw new Error("Invalid layout file: no known layout profiles found.");
  }

  let profile_key = typeof obj.profile_key === "string" ? obj.profile_key.trim() : "";
  if (scope === "single") {
    if (!profile_key || !profiles[profile_key]) {
      // Fall back to the sole profile entry.
      const keys = Object.keys(profiles);
      if (keys.length !== 1) {
        throw new Error("Invalid single-layout file: profile_key missing or does not match profiles.");
      }
      profile_key = keys[0];
    }
    return {
      format: LAYOUT_SHARE_FORMAT,
      format_version: formatVersion,
      layout_doc_version: layoutDocVersion,
      exported_at: typeof obj.exported_at === "string" ? obj.exported_at : new Date().toISOString(),
      scope: "single",
      profile_key,
      profiles: { [profile_key]: profiles[profile_key] },
    };
  }

  return {
    format: LAYOUT_SHARE_FORMAT,
    format_version: formatVersion,
    layout_doc_version: layoutDocVersion,
    exported_at: typeof obj.exported_at === "string" ? obj.exported_at : new Date().toISOString(),
    scope: "all",
    profile_key: profile_key && profiles[profile_key] ? profile_key : undefined,
    profiles,
  };
}

export function layoutExportFilename(scope: LayoutShareScope, activeKey: string): string {
  if (scope === "all") return "placeholdarr-layouts.json";
  const parsed = parseLayoutProfileKey(activeKey);
  if (!parsed) return "placeholdarr-layout.json";
  const timing = parsed.variant === "coming_soon" ? "coming-soon" : "released";
  return `placeholdarr-layout-${parsed.kind}-${timing}.json`;
}

export function downloadJsonFile(filename: string, payload: unknown): void {
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
