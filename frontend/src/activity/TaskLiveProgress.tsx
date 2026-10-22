/** Shared task live-progress body (generic; art refresh is the first consumer). */

import { useState } from "react";

export type LiveUnitBar = {
  index: number;
  done: number;
  total: number;
  status: "working" | "done";
  label?: string;
};

export type LiveDetailMetric = {
  label: string;
  value: string | number;
};

/** Nested bar under a phase (e.g. one ARR instance). */
export type LivePhaseUnitBar = {
  key: string;
  label: string;
  done: number;
  total: number;
  status: "working" | "done" | "pending" | "skipped" | "failed";
};

/** Persistent phase row (Determination / Updating); unlike batch bars, these stay visible. */
export type LivePhaseBar = {
  key: string;
  label: string;
  done: number;
  total: number;
  status: "working" | "done" | "pending" | "skipped" | "failed";
  /** Count cards under an expanded phase bar. */
  metrics?: LiveDetailMetric[];
  /** Nested bars under this phase (shown while the phase is visible). */
  units?: LivePhaseUnitBar[];
};

export type LiveDetailSection = {
  name: string;
  status: "working" | "done" | "pending" | "failed" | "skipped";
  metrics: LiveDetailMetric[];
};

export type TaskLiveProgressModel = {
  label: string;
  kind?: string;
  stage?: string;
  stageLabel?: string;
  overallPct: number;
  unitsDone: number;
  unitsTotal: number;
  unitLabel?: string;
  /** Phase bars (always kept once present). Empty for tasks that only use overall/units. */
  phases?: LivePhaseBar[];
  activeUnits: LiveUnitBar[];
  detailSections?: LiveDetailSection[];
};

/** @deprecated Prefer TaskLiveProgressModel; kept for Phase 0 import compatibility. */
export type ArtRefreshLiveProgressModel = TaskLiveProgressModel & {
  batchesDone: number;
  batchesTotal: number;
  activeBatches: LiveUnitBar[];
};

/** Idle / bake-off mock: determination stage (matches the long pre-enqueue scan). */
export const TASK_LIVE_DETERMINING_MOCK: TaskLiveProgressModel = {
  label: "Art refresh",
  kind: "art_refresh",
  stage: "determining",
  stageLabel: "Determining",
  overallPct: 41,
  unitsDone: 1204,
  unitsTotal: 2944,
  unitLabel: "placeholders",
  phases: [
    {
      key: "determining",
      label: "Determining",
      done: 1204,
      total: 2944,
      status: "working",
      metrics: [
        { label: "Trigger", value: "Manual" },
        { label: "Scanned", value: "1204 / 2944" },
        { label: "Needs refresh", value: 812 },
      ],
    },
  ],
  activeUnits: [],
  detailSections: [],
};

/** Bake-off mock: mid-refresh with batch bars. */
export const TASK_LIVE_MOCK: TaskLiveProgressModel = {
  label: "Art refresh",
  kind: "art_refresh",
  stage: "updating",
  stageLabel: "Updating",
  overallPct: 37,
  unitsDone: 2870,
  unitsTotal: 7750,
  unitLabel: "placeholders",
  phases: [
    {
      key: "determining",
      label: "Determining",
      done: 2944,
      total: 2944,
      status: "done",
      metrics: [
        { label: "Trigger", value: "Manual" },
        { label: "Scanned", value: "2944 / 2944" },
        { label: "Needs refresh", value: 812 },
      ],
    },
    {
      key: "updating",
      label: "Updating",
      done: 2870,
      total: 7750,
      status: "working",
      metrics: [
        { label: "Trigger", value: "Manual" },
        { label: "Placeholders", value: "2870 / 7750" },
        { label: "Batches", value: "11 / 31" },
        { label: "Created", value: 2841 },
        { label: "Updated", value: 412 },
        { label: "Skipped", value: 89 },
        { label: "Errors", value: 0 },
      ],
    },
  ],
  activeUnits: [
    { index: 11, done: 250, total: 250, status: "done" },
    { index: 12, done: 120, total: 250, status: "working" },
  ],
  detailSections: [],
};

/** @deprecated Use TASK_LIVE_MOCK */
export const ART_REFRESH_LIVE_MOCK: ArtRefreshLiveProgressModel = {
  ...TASK_LIVE_MOCK,
  batchesDone: TASK_LIVE_MOCK.unitsDone,
  batchesTotal: TASK_LIVE_MOCK.unitsTotal,
  activeBatches: TASK_LIVE_MOCK.activeUnits,
};

type Density = "compact" | "comfortable";

/** Shared metric cards used by live progress and legacy phase rows. */
export function TaskLiveDetailSections(props: {
  sections: LiveDetailSection[];
  density?: Density;
  isStudioGlass?: boolean;
  /** ``cards`` = metric tiles (default); ``list`` = stacked label/value rows. */
  metricsLayout?: "cards" | "list";
}) {
  const density = props.density ?? "comfortable";
  const compact = density === "compact";
  const glass = props.isStudioGlass !== false;
  const muted = glass ? "text-slate-400" : "text-slate-600";
  const fg = glass ? "text-slate-100" : "text-slate-900";
  const sectionBorder = glass ? "border-[#424753]/45 bg-[#1a2028]/80" : "border-[#c5d0de] bg-white/70";
  const tileBorder = glass ? "border-[#424753]/40 bg-[#141a24]/90" : "border-[#c5d0de] bg-white/80";
  const layout = props.metricsLayout ?? "cards";
  if (!props.sections.length) return null;
  return (
    <div className={compact ? "space-y-2" : "space-y-2.5"}>
      {props.sections.map((section) => (
        <div key={section.name} className={`rounded-lg border p-2.5 ${sectionBorder}`}>
          <div className={`mb-2 flex items-center justify-between gap-2 ${compact ? "text-[11px]" : "text-[12px]"}`}>
            <span className={`font-headline uppercase tracking-wider ${fg}`}>{section.name}</span>
            <span className={`font-headline uppercase tracking-wider ${muted}`}>{section.status}</span>
          </div>
          {layout === "list" ? (
            <ul className="space-y-1">
              {section.metrics.map((metric) => (
                <li key={metric.label} className={`flex items-center justify-between gap-2 ${compact ? "text-[12px]" : "text-[13px]"}`}>
                  <span className={muted}>{metric.label}</span>
                  <span className={`tabular-nums ${fg}`}>{metric.value}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className={`grid items-stretch gap-2 ${compact ? "grid-cols-2" : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-5"}`}>
              {section.metrics.map((metric) => (
                <MetricTile
                  key={metric.label}
                  label={metric.label}
                  value={metric.value}
                  compact={compact}
                  muted={muted}
                  fg={fg}
                  tileBorder={tileBorder}
                />
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function phaseBarPct(done: number, total: number, status: LivePhaseBar["status"]): number {
  if (status === "pending" || status === "skipped") return 0;
  // Unknown total (e.g. FS walk): empty while working; full when the phase has ended.
  if (total <= 0) return status === "done" || status === "failed" ? 100 : 0;
  if (status === "done" || done >= total) return 100;
  // Stopped/failed: keep the frozen done/total fraction (do not paint a full bar).
  if (status === "failed") {
    if (done <= 0) return 0;
    return Math.min(100, Math.floor((100 * done) / total));
  }
  // Working: never paint a full bar from rounding (e.g. 249/250 → 100%).
  return Math.min(99, Math.floor((100 * done) / total));
}

/** Count card: two-line label slot, value always on the third line. */
function MetricTile(props: {
  label: string;
  value: string | number;
  compact: boolean;
  muted: string;
  fg: string;
  tileBorder: string;
}) {
  const { compact, muted, fg, tileBorder } = props;
  return (
    <div className={`flex h-full min-h-0 flex-col rounded-md border px-2.5 py-2 ${tileBorder}`}>
      <p
        className={`line-clamp-2 font-headline uppercase tracking-wider leading-snug ${
          compact ? "min-h-[2.5rem] text-[10px]" : "min-h-[2.75rem] text-[11px]"
        } ${muted}`}
      >
        {props.label}
      </p>
      <p className={`mt-auto pt-1 tabular-nums leading-tight ${compact ? "text-[15px]" : "text-[17px]"} ${fg}`}>
        {props.value}
      </p>
    </div>
  );
}

function PhaseMetricTiles(props: {
  metrics: LiveDetailMetric[];
  density: Density;
  glass: boolean;
}) {
  const compact = props.density === "compact";
  const muted = props.glass ? "text-slate-400" : "text-slate-600";
  const fg = props.glass ? "text-slate-100" : "text-slate-900";
  const tileBorder = props.glass ? "border-[#424753]/40 bg-[#141a24]/90" : "border-[#c5d0de] bg-white/80";
  if (!props.metrics.length) return null;
  return (
    <div className={`grid items-stretch gap-2 ${compact ? "grid-cols-2" : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-5"}`}>
      {props.metrics.map((metric) => (
        <MetricTile
          key={metric.label}
          label={metric.label}
          value={metric.value}
          compact={compact}
          muted={muted}
          fg={fg}
          tileBorder={tileBorder}
        />
      ))}
    </div>
  );
}

function phaseCountsLabel(phase: Pick<LivePhaseBar, "done" | "total" | "status">): string {
  if (phase.status === "skipped") return "—% · —/—";
  if (phase.status === "pending") {
    // Queued with a known size (e.g. batches waiting on workers).
    if (phase.total > 0) return `0% · 0/${phase.total}`;
    return "—% · —/—";
  }
  // Mid-phase with no known total yet (filesystem walk): show the count, not a fake 1/1.
  if (phase.total <= 0) {
    if (phase.status === "done") {
      return phase.done > 0 ? `100% · ${phase.done}` : "100%";
    }
    return phase.done > 0 ? `${phase.done} · …` : "…";
  }
  const pct = phaseBarPct(phase.done, phase.total, phase.status);
  return `${pct}% · ${phase.done}/${phase.total}`;
}

/** Split "Radarr · radarr_secondary" into kind + instance for compact sidebar rows. */
function splitPhaseUnitLabel(label: string): { kind: string; instance: string } | null {
  const raw = String(label || "").trim();
  const sep = " · ";
  const at = raw.indexOf(sep);
  if (at <= 0) return null;
  const kind = raw.slice(0, at).trim();
  const instance = raw.slice(at + sep.length).trim();
  if (!kind || !instance) return null;
  return { kind, instance };
}

export function TaskLiveProgress(props: {
  model: TaskLiveProgressModel;
  density?: Density;
  isStudioGlass?: boolean;
  /** When true, render metric sections under the bars (or inside expandable phases). */
  showFullDetails?: boolean;
}) {
  const density = props.density ?? "comfortable";
  const compact = density === "compact";
  const glass = props.isStudioGlass !== false;
  const muted = glass ? "text-slate-400" : "text-slate-600";
  const fg = glass ? "text-slate-100" : "text-slate-900";
  const track = glass ? "bg-[#2a3340]" : "bg-slate-200";
  const fill = glass ? "bg-sky-400" : "bg-sky-600";
  const fillDone = glass ? "bg-emerald-400" : "bg-emerald-600";
  const fillFailed = glass ? "bg-amber-400" : "bg-amber-600";
  const nestBorder = glass ? "border-[#424753]/45 bg-[#1a2028]/80" : "border-[#c5d0de] bg-white/70";

  const stage = String(props.model.stage || "").toLowerCase();
  const determining = stage === "determining";
  const phases = props.model.phases ?? [];
  const updatingPhase = phases.find((phase) => phase.key === "updating");
  const updatingLive = !updatingPhase || updatingPhase.status === "working";
  // Batch bars are ephemeral while Updating is live; hide them once Updating is done/skipped
  // (and for older payloads that still stored finished batches).
  const units = updatingLive
    ? [...props.model.activeUnits].sort((a, b) => a.index - b.index)
    : [];
  const details = props.showFullDetails ? props.model.detailSections ?? [] : [];
  const unitLabel = props.model.unitLabel || "batches";
  const hasPhases = phases.length > 0;
  // Phase count cards are part of the live model: expand whenever metrics exist.
  // showFullDetails only controls leftover legacy detailSections below the bars.

  // Count cards stay collapsed until the user expands a phase (no auto-expand).
  const [expandedKeys, setExpandedKeys] = useState<Record<string, boolean>>({});

  const togglePhase = (key: string) => {
    setExpandedKeys((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  return (
    <div className={`min-w-0 ${compact ? "space-y-2.5" : "space-y-3"}`}>
      <div className={`flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1 ${compact ? "text-[12px]" : "text-[13px]"}`}>
        <span className={`min-w-0 truncate font-headline uppercase tracking-wider ${fg}`}>{props.model.label}</span>
        {stage === "queued" ? (
          <span className={`font-headline uppercase tracking-wider ${muted}`}>Queued</span>
        ) : null}
      </div>
      {stage === "queued" ? (
        <p className={`${compact ? "text-[11px]" : "text-[12px]"} ${muted}`}>
          Waiting for a worker. Other tasks may still be running.
        </p>
      ) : null}
      {hasPhases ? (
        <ul className={`min-w-0 ${compact ? "space-y-2" : "space-y-2.5"}`}>
          {phases.map((phase) => {
            const pct = phaseBarPct(phase.done, phase.total, phase.status);
            const working = phase.status === "working";
            const failed = phase.status === "failed";
            const done = phase.status === "done";
            const pending = phase.status === "pending";
            const skipped = phase.status === "skipped";
            const statusNote = done
              ? "done"
              : pending
                ? "pending"
                : skipped
                  ? "skipped"
                  : failed
                    ? "stopped"
                    : null;
            const metrics = phase.metrics ?? [];
            const canExpand = metrics.length > 0;
            const expanded = canExpand && Boolean(expandedKeys[phase.key]);
            return (
              <li key={phase.key} className="space-y-1 min-w-0">
                <div
                  className={`flex gap-x-2 gap-y-0.5 ${compact ? "flex-col text-[11px]" : "items-center justify-between text-[12px]"}`}
                >
                  {canExpand ? (
                    <button
                      type="button"
                      onClick={() => togglePhase(phase.key)}
                      className={`inline-flex min-w-0 max-w-full items-center gap-1 text-left font-headline uppercase tracking-wider ${
                        working ? fg : muted
                      }`}
                      aria-expanded={expanded}
                    >
                      <span className="material-symbols-outlined shrink-0 opacity-70" style={{ fontSize: 14 }}>
                        {expanded ? "expand_less" : "expand_more"}
                      </span>
                      <span className="min-w-0 truncate">
                        {phase.label}
                        {statusNote ? (
                          <span className="ml-1.5 normal-case tracking-normal opacity-70">{statusNote}</span>
                        ) : null}
                      </span>
                    </button>
                  ) : (
                    <span className={`min-w-0 truncate font-headline uppercase tracking-wider ${working ? fg : muted}`}>
                      {phase.label}
                      {statusNote ? (
                        <span className="ml-1.5 normal-case tracking-normal opacity-70">{statusNote}</span>
                      ) : null}
                    </span>
                  )}
                  <span className={`tabular-nums ${compact ? "self-end" : "shrink-0"} ${muted}`}>
                    {phaseCountsLabel(phase)}
                  </span>
                </div>
                <div className={`h-1.5 w-full overflow-hidden rounded-full ${track}`}>
                  <div
                    className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                      done ? fillDone : failed ? fillFailed : pending || skipped ? track : fill
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                {(phase.units?.length ?? 0) > 0 ? (
                  <ul className={`ml-2 border-l pl-3 ${compact ? "space-y-1.5" : "space-y-2"} ${glass ? "border-[#424753]/40" : "border-[#c5d0de]"}`}>
                    {phase.units!.map((unit) => {
                      const unitPct = phaseBarPct(unit.done, unit.total, unit.status);
                      const unitWorking = unit.status === "working";
                      const unitDone = unit.status === "done";
                      const unitFailed = unit.status === "failed";
                      const unitPending = unit.status === "pending";
                      const unitSkipped = unit.status === "skipped";
                      const unitNote = unitDone
                        ? "done"
                        : unitPending
                          ? "pending"
                          : unitSkipped
                            ? "skipped"
                            : unitFailed
                              ? "stopped"
                              : null;
                      const split = compact ? splitPhaseUnitLabel(unit.label) : null;
                      return (
                        <li key={unit.key} className="space-y-1 min-w-0">
                          {split ? (
                            <div className={`min-w-0 space-y-0.5 text-[10px]`}>
                              <div className={`min-w-0 truncate font-headline uppercase tracking-wider ${unitWorking ? fg : muted}`}>
                                {split.instance}
                                {unitNote ? (
                                  <span className="ml-1.5 normal-case tracking-normal opacity-70">{unitNote}</span>
                                ) : null}
                              </div>
                              <div className="flex items-baseline justify-between gap-2">
                                <span className={`shrink-0 font-headline uppercase tracking-wider ${unitWorking ? fg : muted}`}>
                                  {split.kind}
                                </span>
                                <span className={`min-w-0 truncate text-right tabular-nums ${muted}`}>
                                  {phaseCountsLabel(unit)}
                                </span>
                              </div>
                            </div>
                          ) : (
                            <div
                              className={`flex gap-x-2 gap-y-0.5 ${compact ? "flex-col text-[10px]" : "items-center justify-between text-[11px]"}`}
                            >
                              <span className={`min-w-0 truncate font-headline uppercase tracking-wider ${unitWorking ? fg : muted}`}>
                                {unit.label}
                                {unitNote ? (
                                  <span className="ml-1.5 normal-case tracking-normal opacity-70">{unitNote}</span>
                                ) : null}
                              </span>
                              <span className={`tabular-nums ${compact ? "self-end" : "shrink-0"} ${muted}`}>
                                {phaseCountsLabel(unit)}
                              </span>
                            </div>
                          )}
                          <div className={`h-1 w-full overflow-hidden rounded-full ${track}`}>
                            <div
                              className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                                unitDone ? fillDone : unitFailed ? fillFailed : unitPending || unitSkipped ? track : fill
                              }`}
                              style={{ width: `${unitPct}%` }}
                            />
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
                {expanded ? (
                  <div className={`rounded-lg border p-2.5 ${nestBorder}`}>
                    <PhaseMetricTiles metrics={metrics} density={density} glass={glass} />
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <>
          <div className={`flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1 ${compact ? "text-[12px]" : "text-[13px]"}`}>
            <span className={`tabular-nums ${muted}`}>
              {props.model.overallPct}% · {props.model.unitsDone}/{props.model.unitsTotal} {unitLabel}
            </span>
          </div>
          <div className={`h-1.5 w-full overflow-hidden rounded-full ${track}`}>
            <div
              className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                props.model.overallPct >= 100 ? fillDone : fill
              }`}
              style={{ width: `${Math.max(0, Math.min(100, props.model.overallPct))}%` }}
            />
          </div>
        </>
      )}
      {determining && !units.length && !hasPhases ? (
        <p className={`${compact ? "text-[11px]" : "text-[12px]"} ${muted}`}>
          Scanning placeholders to see which art needs a refresh. Batches start after this step.
        </p>
      ) : null}
      {units.length ? (
        <ul className={compact ? "space-y-2" : "space-y-2.5"}>
          {units.map((unit) => {
            const working = unit.status === "working";
            const finished = unit.status === "done" || (unit.total > 0 && unit.done >= unit.total);
            const pct =
              unit.total <= 0
                ? finished
                  ? 100
                  : 0
                : finished
                  ? 100
                  : Math.min(99, Math.floor((100 * unit.done) / unit.total));
            const name = unit.label || (compact ? `B${unit.index}` : `Batch ${unit.index}`);
            return (
              <li key={unit.index} className="space-y-1">
                <div className={`flex items-center justify-between gap-2 ${compact ? "text-[11px]" : "text-[12px]"}`}>
                  <span className={`font-headline uppercase tracking-wider ${working && !finished ? fg : muted}`}>
                    {name}
                    {finished ? (
                      <span className="ml-1.5 normal-case tracking-normal opacity-70">done</span>
                    ) : null}
                  </span>
                  <span className={`tabular-nums ${muted}`}>
                    {unit.done}/{unit.total}
                  </span>
                </div>
                <div className={`h-1.5 w-full overflow-hidden rounded-full ${track}`}>
                  <div
                    className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                      finished ? fillDone : fill
                    }`}
                    style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      {details.length ? (
        <div className={compact ? "pt-1" : "pt-1"}>
          <TaskLiveDetailSections sections={details} density={density} isStudioGlass={glass} />
        </div>
      ) : null}
    </div>
  );
}

/** @deprecated Prefer TaskLiveProgress */
export function ArtRefreshLiveProgress(props: {
  model: TaskLiveProgressModel | ArtRefreshLiveProgressModel;
  density?: Density;
  isStudioGlass?: boolean;
  showFullDetails?: boolean;
}) {
  const raw = props.model as ArtRefreshLiveProgressModel;
  const model: TaskLiveProgressModel = {
    label: raw.label,
    kind: raw.kind,
    stage: raw.stage,
    stageLabel: raw.stageLabel,
    overallPct: raw.overallPct,
    unitsDone: raw.unitsDone ?? raw.batchesDone,
    unitsTotal: raw.unitsTotal ?? raw.batchesTotal,
    unitLabel: raw.unitLabel || "batches",
    phases: raw.phases,
    activeUnits: raw.activeUnits ?? raw.activeBatches ?? [],
    detailSections: raw.detailSections,
  };
  return (
    <TaskLiveProgress
      model={model}
      density={props.density}
      isStudioGlass={props.isStudioGlass}
      showFullDetails={props.showFullDetails}
    />
  );
}
