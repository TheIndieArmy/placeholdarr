/** Shared task live-progress body (generic; art refresh is the first consumer). */

export type LiveUnitBar = {
  index: number;
  done: number;
  total: number;
  status: "working" | "done";
  label?: string;
};

/** Persistent phase row (Determination / Updating); unlike batch bars, these stay visible. */
export type LivePhaseBar = {
  key: string;
  label: string;
  done: number;
  total: number;
  status: "working" | "done" | "pending" | "skipped";
};

export type LiveDetailMetric = {
  label: string;
  value: string | number;
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
  phases: [{ key: "determining", label: "Determining", done: 1204, total: 2944, status: "working" }],
  activeUnits: [],
  detailSections: [
    {
      name: "Determination",
      status: "working",
      metrics: [
        { label: "Trigger", value: "Manual" },
        { label: "Scanned", value: "1204 / 2944" },
        { label: "Needs refresh", value: 812 },
      ],
    },
  ],
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
    { key: "determining", label: "Determining", done: 2944, total: 2944, status: "done" },
    { key: "updating", label: "Updating", done: 2870, total: 7750, status: "working" },
  ],
  activeUnits: [
    { index: 11, done: 250, total: 250, status: "done" },
    { index: 12, done: 120, total: 250, status: "working" },
  ],
  detailSections: [
    {
      name: "Determination",
      status: "done",
      metrics: [
        { label: "Trigger", value: "Manual" },
        { label: "Scanned", value: "2944 / 2944" },
        { label: "Needs refresh", value: 812 },
      ],
    },
    {
      name: "Run",
      status: "working",
      metrics: [
        { label: "Trigger", value: "Manual" },
        { label: "Placeholders", value: "2870 / 7750" },
        { label: "Batches", value: "11 / 31" },
      ],
    },
    {
      name: "Outcomes",
      status: "working",
      metrics: [
        { label: "Created", value: 2841 },
        { label: "Updated", value: 412 },
        { label: "Skipped", value: 89 },
        { label: "Errors", value: 0 },
      ],
    },
  ],
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
            <div className={`grid gap-2 ${compact ? "grid-cols-2" : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-5"}`}>
              {section.metrics.map((metric) => (
                <div key={metric.label} className={`rounded-md border px-2.5 py-2 ${tileBorder}`}>
                  <p className={`font-headline uppercase tracking-wider ${compact ? "text-[10px]" : "text-[11px]"} ${muted}`}>
                    {metric.label}
                  </p>
                  <p className={`mt-1 tabular-nums leading-tight ${compact ? "text-[15px]" : "text-[17px]"} ${fg}`}>
                    {metric.value}
                  </p>
                </div>
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
  if (total <= 0) return status === "done" ? 100 : 0;
  if (status === "done" || done >= total) return 100;
  // Working: never paint a full bar from rounding (e.g. 249/250 → 100%).
  return Math.min(99, Math.floor((100 * done) / total));
}

function phaseCountsLabel(phase: LivePhaseBar): string {
  if (phase.status === "skipped") return "—% · —/—";
  if (phase.status === "pending") {
    // Queued with a known size (e.g. batches waiting on workers).
    if (phase.total > 0) return `0% · 0/${phase.total}`;
    return "—% · —/—";
  }
  const pct = phaseBarPct(phase.done, phase.total, phase.status);
  return `${pct}% · ${phase.done}/${phase.total}`;
}

export function TaskLiveProgress(props: {
  model: TaskLiveProgressModel;
  density?: Density;
  isStudioGlass?: boolean;
  /** When true, render metric sections under the bars. */
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

  return (
    <div className={compact ? "space-y-2.5" : "space-y-3"}>
      <div className={`flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1 ${compact ? "text-[12px]" : "text-[13px]"}`}>
        <span className={`font-headline uppercase tracking-wider ${fg}`}>{props.model.label}</span>
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
        <ul className={compact ? "space-y-2" : "space-y-2.5"}>
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
            return (
              <li key={phase.key} className="space-y-1">
                <div className={`flex items-center justify-between gap-2 ${compact ? "text-[11px]" : "text-[12px]"}`}>
                  <span className={`font-headline uppercase tracking-wider ${working ? fg : muted}`}>
                    {phase.label}
                    {statusNote ? (
                      <span className="ml-1.5 normal-case tracking-normal opacity-70">{statusNote}</span>
                    ) : null}
                  </span>
                  <span className={`tabular-nums ${muted}`}>{phaseCountsLabel(phase)}</span>
                </div>
                <div className={`h-1.5 w-full overflow-hidden rounded-full ${track}`}>
                  <div
                    className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                      done ? fillDone : pending || skipped ? track : fill
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
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
