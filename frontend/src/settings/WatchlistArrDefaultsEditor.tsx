import { type ReactNode, useEffect, useMemo, useState } from "react";
import { getCollectionArrAddOptions } from "../api/collections";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import type { CollectionArrAddInstanceOptions } from "../types/api";

export const WATCHLIST_ARR_DEFAULT_KEYS = [
  "WATCHLIST_RADARR_TARGETS_JSON",
  "WATCHLIST_SONARR_TARGETS_JSON",
] as const;

/** Keys owned by the Arr targets section (not the sync-behavior frame). */
export const WATCHLIST_ARR_SECTION_KEYS = [
  ...WATCHLIST_ARR_DEFAULT_KEYS,
  "WATCHLIST_ARR_TAGS",
  "WATCHLIST_TV_REQUEST_SCOPE",
  // Legacy single-target keys (migrated; keep out of sync frame).
  "WATCHLIST_RADARR_INSTANCE_KEY",
  "WATCHLIST_RADARR_QUALITY_PROFILE_ID",
  "WATCHLIST_RADARR_ROOT_FOLDER",
  "WATCHLIST_SONARR_INSTANCE_KEY",
  "WATCHLIST_SONARR_QUALITY_PROFILE_ID",
  "WATCHLIST_SONARR_ROOT_FOLDER",
] as const;

export type WatchlistArrTarget = {
  instance_key: string;
  quality_profile_id: number;
  root_folder_path: string;
};

type MediaKind = "movie" | "show";

type KindMeta = {
  jsonKey: string;
  title: string;
  arrLabel: string;
};

const KIND_META: Record<MediaKind, KindMeta> = {
  movie: {
    jsonKey: "WATCHLIST_RADARR_TARGETS_JSON",
    title: "Movies",
    arrLabel: "Radarr",
  },
  show: {
    jsonKey: "WATCHLIST_SONARR_TARGETS_JSON",
    title: "TV",
    arrLabel: "Sonarr",
  },
};

export function parseWatchlistTargetsJson(raw: unknown): WatchlistArrTarget[] {
  if (Array.isArray(raw)) {
    return normalizeTargetList(raw);
  }
  const text = String(raw ?? "").trim();
  if (!text) return [];
  try {
    const data = JSON.parse(text) as unknown;
    return Array.isArray(data) ? normalizeTargetList(data) : [];
  } catch {
    return [];
  }
}

function normalizeTargetList(raw: unknown[]): WatchlistArrTarget[] {
  const out: WatchlistArrTarget[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const key = String(row.instance_key ?? "").trim();
    if (!key || seen.has(key.toLowerCase())) continue;
    seen.add(key.toLowerCase());
    const qpRaw = row.quality_profile_id;
    const qp =
      qpRaw === null || qpRaw === undefined || qpRaw === "" || qpRaw === 0 || qpRaw === "0"
        ? 0
        : Number(qpRaw);
    out.push({
      instance_key: key,
      quality_profile_id: Number.isFinite(qp) && qp > 0 ? Math.trunc(qp) : 0,
      root_folder_path: String(row.root_folder_path ?? row.root_folder ?? "").trim(),
    });
  }
  return out;
}

export function serializeWatchlistTargets(targets: WatchlistArrTarget[]): string {
  return JSON.stringify(
    targets.map((t) => ({
      instance_key: t.instance_key,
      quality_profile_id: t.quality_profile_id > 0 ? t.quality_profile_id : 0,
      root_folder_path: t.root_folder_path || "",
    })),
  );
}

/** True when an enabled target is missing profile or root (blocks Settings save). */
export function watchlistArrTargetsIncomplete(values: Record<string, unknown>): boolean {
  for (const jsonKey of WATCHLIST_ARR_DEFAULT_KEYS) {
    for (const target of parseWatchlistTargetsJson(values[jsonKey])) {
      if (!(target.quality_profile_id > 0) || !target.root_folder_path.trim()) {
        return true;
      }
    }
  }
  return false;
}

function RequiredBadge(props: { accentHex?: string }) {
  const hex = props.accentHex || "var(--brand-accent)";
  return (
    <span
      className="px-1.5 py-0.5 rounded text-[11px] font-bold font-headline uppercase"
      style={{
        backgroundColor:
          typeof hex === "string" && hex.startsWith("#")
            ? `${hex}4d`
            : "color-mix(in srgb, var(--brand-accent) 30%, transparent)",
        color: hex.startsWith("#") ? hex : "var(--brand-accent)",
      }}
    >
      Required
    </span>
  );
}

function KindTargets(props: {
  kind: MediaKind;
  values: Record<string, unknown>;
  disabled?: boolean;
  accentHex?: string;
  onChange: (key: string, value: unknown) => void;
  footerSlot?: ReactNode;
}) {
  const meta = KIND_META[props.kind];
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [instances, setInstances] = useState<CollectionArrAddInstanceOptions[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getCollectionArrAddOptions(props.kind)
      .then((data) => {
        if (cancelled) return;
        setInstances(data.instances || []);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : `Failed to load ${meta.arrLabel} options`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [meta.arrLabel, props.kind]);

  const targets = useMemo(
    () => parseWatchlistTargetsJson(props.values[meta.jsonKey]),
    [meta.jsonKey, props.values],
  );

  const targetByKey = useMemo(() => {
    const map = new Map<string, WatchlistArrTarget>();
    for (const t of targets) map.set(t.instance_key, t);
    return map;
  }, [targets]);

  const persist = (next: WatchlistArrTarget[]) => {
    props.onChange(meta.jsonKey, serializeWatchlistTargets(next));
  };

  const toggleInstance = (instance: CollectionArrAddInstanceOptions) => {
    const key = instance.instance_key;
    if (targetByKey.has(key)) {
      persist(targets.filter((t) => t.instance_key !== key));
      return;
    }
    // Leave profile/root empty until the user picks them (no first-item assumption).
    persist([
      ...targets,
      { instance_key: key, quality_profile_id: 0, root_folder_path: "" },
    ]);
  };

  const updateTarget = (key: string, patch: Partial<WatchlistArrTarget>) => {
    persist(
      targets.map((t) => (t.instance_key === key ? { ...t, ...patch } : t)),
    );
  };

  return (
    <div className="rounded-xl border border-[#424753]/40 bg-[#12161c] px-4 py-4 space-y-3">
      <div>
        <div className="text-[13px] font-headline uppercase tracking-widest text-slate-400">{meta.title}</div>
        <p className="text-[13px] text-slate-500 mt-1">
          Turn on each {meta.arrLabel} instance that should receive watchlist adds. Profile and root apply per
          instance.
        </p>
      </div>
      {loading ? (
        <p className="text-[13px] text-slate-500">Loading {meta.arrLabel} options…</p>
      ) : error ? (
        <p className="text-[13px] text-red-400">{error}</p>
      ) : instances.length === 0 ? (
        <p className="text-[13px] text-slate-500">
          No {meta.arrLabel} instances configured. Add one under ARR Integrations.
        </p>
      ) : (
        <div className="space-y-3">
          {instances.map((instance) => {
            const enabled = targetByKey.has(instance.instance_key);
            const target = targetByKey.get(instance.instance_key);
            return (
              <div
                key={instance.instance_key}
                className="rounded-lg border border-[#424753]/35 bg-[#0f1419]/80 px-3 py-3 space-y-2"
              >
                <label className="flex items-center gap-2 text-[14px] text-slate-200">
                  <BrandCheckbox
                    checked={enabled}
                    size="sm"
                    accentHex={props.accentHex}
                    disabled={props.disabled}
                    ariaLabel={`${instance.label || instance.instance_key} watchlist target`}
                    onChange={() => {
                      if (!props.disabled) toggleInstance(instance);
                    }}
                  />
                  <span className="min-w-0 truncate">{instance.label || instance.instance_key}</span>
                </label>
                {enabled && target ? (
                  <div className="grid gap-2 sm:grid-cols-1 pl-6">
                    <label className="block space-y-1">
                      <span className="flex items-center gap-2 flex-wrap">
                        <span className="text-[12px] uppercase tracking-widest text-slate-500">Quality profile</span>
                        {!(target.quality_profile_id > 0) ? (
                          <RequiredBadge accentHex={props.accentHex} />
                        ) : null}
                      </span>
                      <BrandSelect
                        fullWidth
                        disabled={props.disabled}
                        value={
                          target.quality_profile_id > 0 ? String(target.quality_profile_id) : ""
                        }
                        options={instance.quality_profiles.map((p) => ({
                          value: String(p.id),
                          label: p.name,
                        }))}
                        onChange={(value) =>
                          updateTarget(instance.instance_key, {
                            quality_profile_id: value ? Number(value) : 0,
                          })
                        }
                        ariaLabel={`${instance.label} quality profile`}
                      />
                    </label>
                    <label className="block space-y-1">
                      <span className="flex items-center gap-2 flex-wrap">
                        <span className="text-[12px] uppercase tracking-widest text-slate-500">Root folder</span>
                        {!target.root_folder_path.trim() ? (
                          <RequiredBadge accentHex={props.accentHex} />
                        ) : null}
                      </span>
                      <BrandSelect
                        fullWidth
                        disabled={props.disabled}
                        value={target.root_folder_path}
                        options={instance.root_folders.map((r) => ({
                          value: r.path,
                          label: r.path,
                        }))}
                        onChange={(value) =>
                          updateTarget(instance.instance_key, { root_folder_path: value })
                        }
                        ariaLabel={`${instance.label} root folder`}
                      />
                    </label>
                  </div>
                ) : null}
              </div>
            );
          })}
          {targets.length === 0 ? (
            <p className="text-[12px] text-slate-500">
              No {meta.arrLabel} targets enabled. Watchlist movie/TV adds for this type will fail until you turn
              at least one on.
            </p>
          ) : null}
        </div>
      )}
      {props.footerSlot ? <div className="pt-1 border-t border-[#424753]/30 space-y-2">{props.footerSlot}</div> : null}
    </div>
  );
}

export function WatchlistArrDefaultsEditor(props: {
  values: Record<string, unknown>;
  disabled?: boolean;
  accentHex?: string;
  onChange: (key: string, value: unknown) => void;
  /** Shared tags control rendered above the Movies/TV cards. */
  tagsSlot?: ReactNode;
  /** TV-only control (request scope) rendered inside the TV card. */
  tvScopeSlot?: ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div>
        <div className="text-[13px] font-headline uppercase tracking-widest text-slate-400">
          Arr add targets
        </div>
        <p className="text-[13px] text-slate-500 mt-1">
          Watchlist auto-add and Add to Arr send each title to every instance you enable here.
        </p>
      </div>
      {props.tagsSlot ? <div className="space-y-1">{props.tagsSlot}</div> : null}
      <div className="grid gap-3 lg:grid-cols-2">
        <KindTargets
          kind="movie"
          values={props.values}
          disabled={props.disabled}
          accentHex={props.accentHex}
          onChange={props.onChange}
        />
        <KindTargets
          kind="show"
          values={props.values}
          disabled={props.disabled}
          accentHex={props.accentHex}
          onChange={props.onChange}
          footerSlot={props.tvScopeSlot}
        />
      </div>
    </div>
  );
}
