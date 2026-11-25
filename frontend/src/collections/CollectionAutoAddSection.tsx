import { useEffect, useMemo, useState } from "react";
import type { CollectionAutoAddConfig } from "../types/api";
import type { CollectionArrAddInstanceOptions } from "../types/api";
import { getCollectionArrAddOptions } from "../api/collections";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import { getCollectionTheme } from "./collectionTheme";
import type { ThemeMode } from "../brandTypes";
import { ToggleSwitch } from "./ToggleSwitch";
import { normalizeAutoAddConfig } from "./collectionAutoAddUtils";

const DEFAULT_AUTO_ADD: CollectionAutoAddConfig = {
  enabled: true,
  instance_keys: [],
  instance_options: {},
  quality_profile_id: 0,
  root_folder_path: "",
  monitored: true,
  search: false,
  tags: ["placeholdarr-discover"],
  missing_limit: 10,
  prune: { enabled: false, grace_days: 14 },
};

function RequiredBadge(props: { accentHex: string }) {
  return (
    <span
      className="px-1.5 py-0.5 rounded text-[11px] font-bold font-headline uppercase"
      style={{
        backgroundColor: props.accentHex.startsWith("#") ? `${props.accentHex}33` : "rgba(255,193,7,0.2)",
        color: props.accentHex.startsWith("#") ? props.accentHex : "rgb(251, 191, 36)",
      }}
    >
      Required
    </span>
  );
}

export function CollectionAutoAddSection(props: {
  themeMode: ThemeMode;
  accentHex: string;
  mediaType: "movie" | "show";
  autoAdd: CollectionAutoAddConfig | null | undefined;
  onChange: (next: CollectionAutoAddConfig | null | undefined) => void;
  scopeHint?: string;
  layout?: "section" | "panel";
  disabledReason?: string;
  /** Omit section title when embedded in CollectionShelfContentsSection. */
  hideHeader?: boolean;
  /** When false, hide instance pickers until Plex library type is known. */
  arrTargetsReady?: boolean;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const layout = props.layout ?? "section";
  const arrLabel = props.mediaType === "movie" ? "Radarr" : "Sonarr";
  const disabled = Boolean(props.disabledReason);
  const arrTargetsReady = props.arrTargetsReady !== false;

  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [instances, setInstances] = useState<CollectionArrAddInstanceOptions[]>([]);

  const config = useMemo(() => normalizeAutoAddConfig(props.autoAdd) ?? null, [props.autoAdd]);
  const instanceKeys = config?.instance_keys ?? [];
  const instanceOptions = config?.instance_options ?? {};

  useEffect(() => {
    if (!arrTargetsReady) {
      setInstances([]);
      setLoading(false);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getCollectionArrAddOptions(props.mediaType)
      .then((data) => {
        if (cancelled) return;
        setInstances(data.instances || []);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : `Failed to load ${arrLabel} options`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [arrLabel, arrTargetsReady, props.mediaType]);

  const patchConfig = (patch: Partial<CollectionAutoAddConfig>) => {
    const base = config ?? DEFAULT_AUTO_ADD;
    props.onChange({ ...base, ...patch });
  };

  const toggleInstance = (instance: CollectionArrAddInstanceOptions) => {
    const key = instance.instance_key;
    const enabled = instanceKeys.includes(key);
    if (enabled) {
      const nextKeys = instanceKeys.filter((k) => k !== key);
      const nextOpts = { ...instanceOptions };
      delete nextOpts[key];
      patchConfig({ instance_keys: nextKeys, instance_options: nextOpts });
      return;
    }
    patchConfig({
      instance_keys: [...instanceKeys, key],
      instance_options: {
        ...instanceOptions,
        [key]: { quality_profile_id: 0, root_folder_path: "" },
      },
    });
  };

  const updateInstance = (
    key: string,
    patch: { quality_profile_id?: number; root_folder_path?: string },
  ) => {
    const cur = instanceOptions[key] || { quality_profile_id: 0, root_folder_path: "" };
    patchConfig({
      instance_options: {
        ...instanceOptions,
        [key]: { ...cur, ...patch },
      },
    });
  };

  const missingLimit =
    props.autoAdd?.missing_limit ?? props.autoAdd?.max_per_run ?? DEFAULT_AUTO_ADD.missing_limit;
  const pruneEnabled = Boolean(props.autoAdd?.prune?.enabled);
  const pruneGrace = props.autoAdd?.prune?.grace_days ?? 14;
  const showPruneSection = instanceKeys.length > 0 || pruneEnabled;

  return (
    <div className={layout === "section" ? `border-t pt-4 ${theme.divider}` : "space-y-3"}>
      {!props.hideHeader && layout === "section" ? (
        <div className={`mb-2 ${theme.blockTitle}`}>Titles you don&apos;t have</div>
      ) : null}
      {!props.hideHeader ? (
        <p className={`mb-3 text-[13px] ${theme.muted}`}>
          When enabled, each collection uses TMDB (when configured) to fill placeholder slots and add missing titles
          to {arrLabel}. New adds use the{" "}
          <code className="font-mono text-[12px]">placeholdarr-discover</code> tag.
        </p>
      ) : null}
      {props.scopeHint && !props.hideHeader ? (
        <p className={`mb-3 text-[13px] ${theme.muted}`}>{props.scopeHint}</p>
      ) : null}
      {props.disabledReason && !props.hideHeader ? (
        <p className={`mb-3 text-[13px] ${isLight ? "text-amber-800" : "text-amber-300"}`}>
          {props.disabledReason}
        </p>
      ) : null}
      <label className={`flex flex-wrap items-center gap-3 text-[13px] ${theme.label} ${disabled ? "opacity-50" : ""}`}>
        <ToggleSwitch
          checked={Boolean(props.autoAdd?.enabled) && !disabled}
          accentHex={props.accentHex}
          ariaLabel="Include titles you do not have"
          disabled={disabled}
          onChange={(enabled) =>
            props.onChange(
              enabled
                ? {
                    ...DEFAULT_AUTO_ADD,
                    ...(config?.enabled ? config : {}),
                    enabled: true,
                  }
                : { enabled: false },
            )
          }
        />
        <span className="shrink-0">Titles you don&apos;t have</span>
        {props.autoAdd?.enabled && !disabled ? (
          <>
            <span className={theme.muted}>up to</span>
            <input
              type="number"
              className={theme.field}
              style={{ width: 90 }}
              value={missingLimit ?? 10}
              min={1}
              max={50}
              onChange={(e) => {
                const raw = e.target.value;
                const parsed = raw === "" ? 10 : Number(raw);
                patchConfig({
                  missing_limit: Number.isFinite(parsed) ? Math.min(50, Math.max(1, parsed)) : 10,
                });
              }}
            />
          </>
        ) : null}
      </label>
      {props.autoAdd?.enabled && !disabled ? (
        <>
          <div className="mt-4 space-y-2">
            {!arrTargetsReady ? (
              <p className={`text-[13px] ${theme.muted}`}>
                Select a Plex library above to load Radarr or Sonarr targets.
              </p>
            ) : null}
            {arrTargetsReady ? (
              <>
            <div className={`text-[12px] font-headline uppercase tracking-widest ${theme.muted}`}>
              {arrLabel} targets
            </div>
            <p className={`text-[12px] ${theme.muted}`}>
              Choose which {arrLabel} instance(s) receive adds, with a quality profile and root folder for each.
            </p>
            {loading ? (
              <p className={`text-[13px] ${theme.muted}`}>Loading {arrLabel} instances…</p>
            ) : loadError ? (
              <p className="text-[13px] text-red-400">{loadError}</p>
            ) : instances.length === 0 ? (
              <p className={`text-[13px] ${theme.muted}`}>
                No {arrLabel} instances configured. Add one under Settings → ARR Integrations.
              </p>
            ) : (
              <div className="space-y-2">
                {instances.map((instance) => {
                  const enabled = instanceKeys.includes(instance.instance_key);
                  const target = instanceOptions[instance.instance_key];
                  return (
                    <div
                      key={instance.instance_key}
                      className={`rounded-lg border px-3 py-3 space-y-2 ${
                        isLight ? "border-slate-200 bg-slate-50/80" : "border-[#424753]/35 bg-[#0f1419]/80"
                      }`}
                    >
                      <label className={`flex items-center gap-2 text-[14px] ${theme.label}`}>
                        <BrandCheckbox
                          checked={enabled}
                          size="sm"
                          accentHex={props.accentHex}
                          ariaLabel={`${instance.label || instance.instance_key} auto-add target`}
                          onChange={() => toggleInstance(instance)}
                        />
                        <span className="min-w-0 truncate">{instance.label || instance.instance_key}</span>
                      </label>
                      {enabled && target ? (
                        <div className="grid gap-2 pl-6">
                          <label className="block space-y-1">
                            <span className="flex items-center gap-2 flex-wrap">
                              <span className={`text-[12px] uppercase tracking-widest ${theme.muted}`}>
                                Quality profile
                              </span>
                              {!(target.quality_profile_id > 0) ? (
                                <RequiredBadge accentHex={props.accentHex} />
                              ) : null}
                            </span>
                            <BrandSelect
                              fullWidth
                              value={target.quality_profile_id > 0 ? String(target.quality_profile_id) : ""}
                              options={[
                                { value: "", label: "Select profile…" },
                                ...instance.quality_profiles.map((p) => ({
                                  value: String(p.id),
                                  label: p.name,
                                })),
                              ]}
                              onChange={(value) =>
                                updateInstance(instance.instance_key, {
                                  quality_profile_id: value ? Number(value) : 0,
                                })
                              }
                              ariaLabel={`${instance.label} quality profile`}
                            />
                          </label>
                          <label className="block space-y-1">
                            <span className="flex items-center gap-2 flex-wrap">
                              <span className={`text-[12px] uppercase tracking-widest ${theme.muted}`}>Root folder</span>
                              {!target.root_folder_path.trim() ? (
                                <RequiredBadge accentHex={props.accentHex} />
                              ) : null}
                            </span>
                            <BrandSelect
                              fullWidth
                              value={target.root_folder_path}
                              options={[
                                { value: "", label: "Select root folder…" },
                                ...instance.root_folders.map((r) => ({
                                  value: r.path,
                                  label: r.path,
                                })),
                              ]}
                              onChange={(value) =>
                                updateInstance(instance.instance_key, { root_folder_path: value })
                              }
                              ariaLabel={`${instance.label} root folder`}
                            />
                          </label>
                        </div>
                      ) : null}
                    </div>
                  );
                })}
                {!instanceKeys.length ? (
                  <p className={`text-[12px] ${theme.muted}`}>
                    Select at least one {arrLabel} instance before enabling this option.
                  </p>
                ) : null}
              </div>
            )}
              </>
            ) : null}
          </div>
          {showPruneSection ? (
            <div className={`mt-4 rounded-lg border px-3 py-3 space-y-2 ${theme.divider}`}>
              <label className={`flex flex-wrap items-center gap-3 text-[13px] ${theme.label}`}>
                <ToggleSwitch
                  checked={pruneEnabled}
                  accentHex={props.accentHex}
                  ariaLabel="Prune never-played auto-adds"
                  onChange={(next) =>
                    patchConfig({
                      prune: { enabled: next, grace_days: pruneGrace },
                    })
                  }
                />
                <span className="shrink-0">Prune never-played auto-adds after grace period</span>
              </label>
              <p className={`text-[12px] ${theme.muted}`}>
                Grace resets while a title is still on this collection&apos;s would-add list. After it drops off and
                nothing was played, it can be removed from Arr.
              </p>
              <label
                className={`flex items-center gap-2 text-[13px] ${theme.label} ${!pruneEnabled ? "opacity-50" : ""}`}
              >
                Grace days
                <input
                  type="number"
                  className={theme.field}
                  style={{ width: 72 }}
                  value={pruneGrace}
                  min={1}
                  max={365}
                  disabled={!pruneEnabled}
                  onChange={(e) => {
                    if (!pruneEnabled) return;
                    const parsed = Number(e.target.value);
                    patchConfig({
                      prune: {
                        enabled: true,
                        grace_days: Number.isFinite(parsed) ? Math.min(365, Math.max(1, parsed)) : 14,
                      },
                    });
                  }}
                />
              </label>
            </div>
          ) : arrTargetsReady ? (
            <p className={`mt-4 text-[12px] ${theme.muted}`}>
              Select at least one {arrLabel} instance above to configure prune.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}