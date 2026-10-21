/** Map API ``summary.live`` / ``TaskRunRow.live`` into the shared progress model. */

import type { LiveDetailMetric, LivePhaseBar, LivePhaseUnitBar, TaskLiveProgressModel } from "./TaskLiveProgress";
import type { TaskRunLiveProgress } from "../types/api";

function parseSlashCounts(raw: unknown): { done: number; total: number } | null {
  const text = String(raw ?? "").trim();
  const match = /^(\d+)\s*\/\s*(\d+)$/.exec(text);
  if (!match) return null;
  return { done: Number(match[1]), total: Number(match[2]) };
}

function metricValue(
  section: { metrics?: Array<{ label?: string; value?: string | number }> } | undefined,
  label: string,
): unknown {
  const needle = label.trim().toLowerCase();
  for (const metric of section?.metrics || []) {
    if (String(metric.label || "").trim().toLowerCase() === needle) return metric.value;
  }
  return undefined;
}

function normalizePhaseStatus(raw: unknown): LivePhaseBar["status"] {
  const status = String(raw || "working").toLowerCase();
  if (
    status === "done" ||
    status === "pending" ||
    status === "working" ||
    status === "skipped" ||
    status === "failed"
  ) {
    return status;
  }
  return "working";
}

function mapMetrics(raw: unknown): LiveDetailMetric[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((metric) => {
      if (!metric || typeof metric !== "object") return null;
      const label = String((metric as { label?: string }).label || "").trim();
      if (!label) return null;
      return {
        label,
        value: (metric as { value?: string | number }).value as string | number,
      } satisfies LiveDetailMetric;
    })
    .filter((metric): metric is LiveDetailMetric => metric != null);
}

function mapPhaseUnits(raw: unknown): LivePhaseUnitBar[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((unit) => {
      if (!unit || typeof unit !== "object") return null;
      const key = String((unit as { key?: string }).key || "")
        .trim()
        .toLowerCase()
        .replace(/\s+/g, "_");
      if (!key) return null;
      const status = normalizePhaseStatus((unit as { status?: string }).status);
      let done = Number((unit as { done?: number }).done ?? 0);
      let total = Number((unit as { total?: number }).total ?? 0);
      if (status === "skipped") {
        done = 0;
        total = 0;
      } else if (status === "pending") {
        done = 0;
      }
      return {
        key,
        label: String((unit as { label?: string }).label || key.replace(/_/g, " ")),
        done,
        total,
        status,
      } satisfies LivePhaseUnitBar;
    })
    .filter((unit): unit is LivePhaseUnitBar => unit != null);
}

function metricNumber(metrics: LiveDetailMetric[], label: string): number | null {
  const needle = label.trim().toLowerCase();
  for (const metric of metrics) {
    if (String(metric.label || "").trim().toLowerCase() !== needle) continue;
    const n = Number(metric.value);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** When a finished bar is still coarse 1/1, recover a real total from count cards. */
function refineDoneTotal(
  key: string,
  status: LivePhaseBar["status"],
  done: number,
  total: number,
  metrics: LiveDetailMetric[],
): { done: number; total: number } {
  if (status !== "done" || !metrics.length) return { done, total };
  const coarse = total <= 1 && done <= 1;
  if (!coarse && total > 0) return { done, total };

  if (key === "fs_scan" || key === "filesystem_scan") {
    const files = metricNumber(metrics, "Files on disk (walked)");
    if (files != null) return { done: files, total: files };
    const indexed = metricNumber(metrics, "New paths indexed");
    if (indexed != null) return { done: indexed, total: indexed };
  }
  if (key === "discovery" || key === "catalog_discovery") {
    const movies =
      metricNumber(metrics, "Movies checked") ?? metricNumber(metrics, "Movies updated") ?? 0;
    const series =
      metricNumber(metrics, "Series checked") ?? metricNumber(metrics, "Series updated") ?? 0;
    const episodes =
      metricNumber(metrics, "Episodes checked") ?? metricNumber(metrics, "Episodes updated") ?? 0;
    const sum = movies + series + episodes;
    if (sum > 0 || coarse) return { done: sum, total: sum };
  }
  if (key === "determination" || key === "status_determination") {
    const movies = metricNumber(metrics, "Movies evaluated") ?? 0;
    const episodes = metricNumber(metrics, "Episodes evaluated") ?? 0;
    let sum = movies + episodes;
    if (sum <= 0) {
      sum =
        (metricNumber(metrics, "Already has placeholder") ?? 0) +
        (metricNumber(metrics, "Not yet aired") ?? 0) +
        (metricNumber(metrics, "Air date unknown") ?? 0);
    }
    if (sum > 0 || coarse) return { done: sum, total: sum };
  }
  if (key === "materialization" || key === "placeholder_materialization") {
    const sum =
      (metricNumber(metrics, "Placeholders created") ?? 0) +
      (metricNumber(metrics, "Placeholders removed") ?? 0) +
      (metricNumber(metrics, "Already up to date") ?? 0) +
      (metricNumber(metrics, "Errors") ?? 0);
    if (sum > 0 || coarse) return { done: sum, total: sum };
  }
  if (key === "calendar" || key === "calendar_status") {
    const updates = metricNumber(metrics, "Calendar updates");
    if (updates != null) return { done: updates, total: updates };
  }
  if (key === "arr_sync" || key === "arr_catalog_sync") {
    let items = 0;
    let found = false;
    for (const metric of metrics) {
      if (String(metric.label || "").trim().toLowerCase() === "total arr sync time") continue;
      const text = String(metric.value ?? "");
      for (const match of text.matchAll(/(\d+)\s+movies/gi)) {
        items += Number(match[1]);
        found = true;
      }
      for (const match of text.matchAll(/(\d+)\s+series/gi)) {
        items += Number(match[1]);
        found = true;
      }
    }
    if (found) return { done: items, total: items };
  }
  return { done, total };
}

function sectionKey(name: string): string {
  return String(name || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "_");
}

function isEmptyMetricValue(value: string | number | undefined): boolean {
  if (value == null) return true;
  if (typeof value === "number") return false;
  const text = String(value).trim();
  if (!text || text === "--" || text === "—" || text === "-" || text === "Pending") return true;
  if (/^[—\-]\s*\/\s*[—\-]$/.test(text)) return true;
  if (/^0\s*\/\s*0$/.test(text)) return true;
  return false;
}

/** Merge count cards; keep existing labels, fill empties, append new labels. */
function mergeMetrics(base: LiveDetailMetric[], extra: LiveDetailMetric[]): LiveDetailMetric[] {
  if (!extra.length) return base;
  if (!base.length) return extra;
  const out = [...base];
  const indexByLabel = new Map(out.map((metric, index) => [metric.label.toLowerCase(), index]));
  for (const metric of extra) {
    const key = metric.label.toLowerCase();
    const at = indexByLabel.get(key);
    if (at == null) {
      indexByLabel.set(key, out.length);
      out.push(metric);
      continue;
    }
    if (isEmptyMetricValue(out[at].value) && !isEmptyMetricValue(metric.value)) {
      out[at] = metric;
    }
  }
  return out;
}

/** Map a legacy detail / progress section name onto a live phase key. */
export function detailSectionPhaseKey(name: string): string | null {
  const key = sectionKey(name);
  // Art refresh legacy cards.
  if (key === "determination") return "determining";
  if (key === "run" || key === "outcomes") return "updating";
  // Full / lite sync legacy cards (pre-phase live model).
  if (key === "scope_checked") return "discovery";
  if (key === "what_changed") return "materialization";
  if (key === "why_items_were_skipped") return "determination";
  if (key === "arr_catalog_sync") return "arr_sync";
  if (key === "filesystem_scan") return "fs_scan";
  if (key === "status_determination") return "determination";
  if (key === "placeholder_materialization") return "materialization";
  if (key === "calendar_status") return "calendar";
  if (key === "catalog_discovery") return "discovery";
  if (key === "calendar_dates") return "calendar_dates";
  return key || null;
}

/** Prefer API ``phases``; fall back to art detail sections for older live payloads. */
export function phasesFromLive(live: TaskRunLiveProgress): LivePhaseBar[] {
  if (Array.isArray(live.phases) && live.phases.length) {
    return live.phases
      .map((phase) => {
        const key = String(phase.key || "")
          .trim()
          .toLowerCase()
          .replace(/\s+/g, "_");
        if (!key) return null;
        const status = normalizePhaseStatus(phase.status);
        let done = Number(phase.done ?? 0);
        let total = Number(phase.total ?? 0);
        if (status === "done" && total <= 0 && key === "updating") {
          // Empty Updating after a full determination is skipped, not 0/0 done.
          return {
            key,
            label: String(phase.label || key.replace(/_/g, " ")),
            done: 0,
            total: 0,
            status: "skipped" as const,
            metrics: mapMetrics(phase.metrics),
            units: mapPhaseUnits(phase.units),
          } satisfies LivePhaseBar;
        }
        if (status === "skipped") {
          done = 0;
          total = 0;
        } else if (status === "pending") {
          done = 0;
          // Keep total when known (queued work waiting on workers).
        }
        const metrics = mapMetrics(phase.metrics);
        const units = mapPhaseUnits(phase.units);
        const refined = refineDoneTotal(key, status, done, total, metrics);
        return {
          key,
          label: String(phase.label || key.replace(/_/g, " ")),
          done: refined.done,
          total: refined.total,
          status,
          metrics,
          units,
        } satisfies LivePhaseBar;
      })
      .filter((phase): phase is LivePhaseBar => phase != null);
  }

  const sections = Array.isArray(live.detail_sections) ? live.detail_sections : [];
  const stage = String(live.stage || "").toLowerCase();
  const out: LivePhaseBar[] = [];

  const determination = sections.find((section) => String(section.name || "").toLowerCase() === "determination");
  if (determination) {
    const scanned = parseSlashCounts(metricValue(determination, "Scanned"));
    const status = normalizePhaseStatus(determination.status);
    out.push({
      key: "determining",
      label: "Determining",
      done: scanned?.done ?? Number(live.units_done ?? 0),
      total: scanned?.total ?? (stage === "determining" ? Number(live.units_total ?? 0) : scanned?.total ?? 0),
      status: status === "pending" ? "working" : status,
      metrics: mapMetrics(determination.metrics),
    });
  } else if (stage === "determining") {
    out.push({
      key: "determining",
      label: "Determining",
      done: Number(live.units_done ?? 0),
      total: Number(live.units_total ?? 0),
      status: "working",
    });
  }

  const run = sections.find((section) => String(section.name || "").toLowerCase() === "run");
  const outcomes = sections.find((section) => String(section.name || "").toLowerCase() === "outcomes");
  if (run || stage === "updating" || stage === "refreshing") {
    const placeholders = parseSlashCounts(metricValue(run, "Placeholders"));
    let status = normalizePhaseStatus(run?.status);
    const done = placeholders?.done ?? Number(live.units_done ?? 0);
    const total = placeholders?.total ?? Number(live.units_total ?? 0);
    if (total <= 0 && stage !== "determining") status = "done";
    if (run || stage === "updating" || stage === "refreshing") {
      out.push({
        key: "updating",
        label: "Updating",
        done,
        total,
        status: status === "pending" ? "working" : status,
        metrics: [...mapMetrics(run?.metrics), ...mapMetrics(outcomes?.metrics)],
      });
    }
  }

  return out;
}

/** Fold legacy progress / detail section metrics into the matching phase bars. */
export function hydratePhaseMetrics(
  phases: LivePhaseBar[],
  sections: Array<{ name?: string; key?: string; status?: string; metrics?: Array<{ label?: string; value?: string | number }> }>,
): LivePhaseBar[] {
  if (!phases.length) return phases;
  return phases.map((phase) => {
    let metrics = phase.metrics ?? [];
    if (sections.length) {
      const matched = sections.filter((section) => {
        const fromName = detailSectionPhaseKey(String(section.name || section.key || ""));
        const fromKey = detailSectionPhaseKey(String(section.key || "")) || sectionKey(String(section.key || ""));
        return fromName === phase.key || fromKey === phase.key;
      });
      if (matched.length) {
        const legacyMetrics = matched.flatMap((section) => mapMetrics(section.metrics));
        metrics = mergeMetrics(metrics, legacyMetrics);
      }
    }
    const refined = refineDoneTotal(phase.key, phase.status, phase.done, phase.total, metrics);
    if (
      metrics === (phase.metrics ?? []) &&
      refined.done === phase.done &&
      refined.total === phase.total
    ) {
      return phase;
    }
    return {
      ...phase,
      done: refined.done,
      total: refined.total,
      metrics: metrics.length ? metrics : phase.metrics,
    };
  });
}

export function taskLiveFromApi(live: TaskRunLiveProgress | null | undefined): TaskLiveProgressModel | null {
  if (!live || typeof live !== "object") return null;
  const unitsDone = Number(live.units_done ?? 0);
  const unitsTotal = Number(live.units_total ?? 0);
  const active = Array.isArray(live.active_units) ? live.active_units : [];
  const stage = live.stage ? String(live.stage) : undefined;
  // Empty Updating queue (0 enqueued after determination) is complete, not 0%.
  let overallPct = Math.max(0, Math.min(100, Number(live.overall_pct ?? 0)));
  if (unitsTotal <= 0 && String(stage || "").toLowerCase() !== "determining") {
    overallPct = 100;
  }
  const detailSections = Array.isArray(live.detail_sections)
    ? live.detail_sections.map((section) => {
        let status = (["working", "done", "pending", "failed", "skipped"].includes(String(section.status))
          ? section.status
          : "working") as "working" | "done" | "pending" | "failed" | "skipped";
        // Empty Updating queue after determination: treat leftover WORKING cards as skipped.
        if (unitsTotal <= 0 && String(stage || "").toLowerCase() !== "determining" && status === "working") {
          const name = String(section.name || "").toLowerCase();
          status = name === "run" || name === "outcomes" ? "skipped" : "done";
        }
        return {
          name: String(section.name || "Section"),
          status,
          metrics: Array.isArray(section.metrics)
            ? section.metrics.map((metric) => ({
                label: String(metric.label || "Metric"),
                value: metric.value as string | number,
              }))
            : [],
        };
      })
    : [];

  let phases = hydratePhaseMetrics(phasesFromLive(live), detailSections).map((phase) => {
    const refined = refineDoneTotal(
      phase.key,
      phase.status,
      phase.done,
      phase.total,
      phase.metrics ?? [],
    );
    if (refined.done === phase.done && refined.total === phase.total) return phase;
    return { ...phase, done: refined.done, total: refined.total };
  });
  // When phases carry the counts, drop the duplicate footer strip.
  const phasesHaveMetrics = phases.some((phase) => (phase.metrics?.length ?? 0) > 0);
  const leftoverDetailSections = phasesHaveMetrics
    ? detailSections.filter((section) => {
        const key = detailSectionPhaseKey(section.name);
        if (!key) return true;
        const phase = phases.find((p) => p.key === key);
        return !(phase && (phase.metrics?.length ?? 0) > 0);
      })
    : detailSections;

  return {
    label: String(live.label || "Task"),
    kind: String(live.kind || "task"),
    stage,
    stageLabel: live.stage_label ? String(live.stage_label) : undefined,
    overallPct,
    unitsDone,
    unitsTotal,
    unitLabel: String(live.unit_label || "batches"),
    phases,
    activeUnits: active.map((unit) => ({
      index: Number(unit.index),
      done: Number(unit.done ?? 0),
      total: Number(unit.total ?? 0),
      status: unit.status === "done" ? "done" : "working",
      label: unit.label ? String(unit.label) : undefined,
    })),
    detailSections: leftoverDetailSections,
  };
}

export function pickWorkingLiveRuns(runs: Array<{ status?: string; live?: TaskRunLiveProgress | null }>): TaskRunLiveProgress[] {
  return runs
    .filter((run) => String(run.status || "").toUpperCase() === "WORKING" && run.live)
    .map((run) => run.live as TaskRunLiveProgress);
}
