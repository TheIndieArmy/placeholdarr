/** Map API ``summary.live`` / ``TaskRunRow.live`` into the shared progress model. */

import type { LivePhaseBar, TaskLiveProgressModel } from "./TaskLiveProgress";
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
          } satisfies LivePhaseBar;
        }
        if (status === "skipped") {
          done = 0;
          total = 0;
        } else if (status === "pending") {
          done = 0;
          // Keep total when known (queued work waiting on workers).
        }
        return {
          key,
          label: String(phase.label || key.replace(/_/g, " ")),
          done,
          total,
          status,
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
      });
    }
  }

  return out;
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

  return {
    label: String(live.label || "Task"),
    kind: String(live.kind || "task"),
    stage,
    stageLabel: live.stage_label ? String(live.stage_label) : undefined,
    overallPct,
    unitsDone,
    unitsTotal,
    unitLabel: String(live.unit_label || "batches"),
    phases: phasesFromLive(live),
    activeUnits: active.map((unit) => ({
      index: Number(unit.index),
      done: Number(unit.done ?? 0),
      total: Number(unit.total ?? 0),
      status: unit.status === "done" ? "done" : "working",
      label: unit.label ? String(unit.label) : undefined,
    })),
    detailSections,
  };
}

export function pickWorkingLiveRuns(runs: Array<{ status?: string; live?: TaskRunLiveProgress | null }>): TaskRunLiveProgress[] {
  return runs
    .filter((run) => String(run.status || "").toUpperCase() === "WORKING" && run.live)
    .map((run) => run.live as TaskRunLiveProgress);
}
