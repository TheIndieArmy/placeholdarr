import type { CollectionAutoAddConfig } from "../types/api";

export type AutoAddInstanceOption = {
  quality_profile_id: number;
  root_folder_path: string;
};

export function normalizeAutoAddConfig(
  raw: CollectionAutoAddConfig | null | undefined,
): CollectionAutoAddConfig | null | undefined {
  if (!raw) return raw;
  if (raw.instance_options && Object.keys(raw.instance_options).length > 0) {
    return raw;
  }
  const keys = raw.instance_keys || [];
  const qp = raw.quality_profile_id ?? 0;
  const root = String(raw.root_folder_path || "").trim();
  if (!keys.length || qp < 1 || !root) return raw;
  const instance_options: Record<string, AutoAddInstanceOption> = {};
  for (const key of keys) {
    instance_options[key] = { quality_profile_id: qp, root_folder_path: root };
  }
  return { ...raw, instance_options };
}

export function isCollectionAutoAddReady(config: CollectionAutoAddConfig | null | undefined): boolean {
  if (!config?.enabled) return true;
  const normalized = normalizeAutoAddConfig(config);
  const keys = normalized?.instance_keys || [];
  const opts = normalized?.instance_options || {};
  if (!keys.length) return false;
  return keys.every((key) => {
    const row = opts[key];
    return Boolean(row && row.quality_profile_id > 0 && row.root_folder_path.trim());
  });
}

export function buildAutoAddPayload(config: CollectionAutoAddConfig | null | undefined): CollectionAutoAddConfig | undefined {
  if (!config?.enabled) {
    return config?.enabled === false ? { enabled: false } : undefined;
  }
  const normalized = normalizeAutoAddConfig(config);
  if (!normalized || !isCollectionAutoAddReady(normalized)) return normalized ?? undefined;
  const keys = normalized.instance_keys || [];
  const opts = normalized.instance_options || {};
  const first = keys[0] ? opts[keys[0]] : undefined;
  return {
    enabled: true,
    instance_keys: keys,
    instance_options: opts,
    quality_profile_id: first?.quality_profile_id ?? 0,
    root_folder_path: first?.root_folder_path ?? "",
    monitored: normalized.monitored ?? true,
    search: normalized.search ?? false,
    tags: normalized.tags?.length ? normalized.tags : ["placeholdarr-discover"],
    missing_limit: normalized.missing_limit ?? normalized.max_per_run ?? 10,
    max_per_run: normalized.missing_limit ?? normalized.max_per_run ?? 10,
    prune: normalized.prune ?? { enabled: false, grace_days: 14 },
  };
}