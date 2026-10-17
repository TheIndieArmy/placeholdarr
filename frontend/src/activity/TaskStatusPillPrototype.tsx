import { useEffect, useId, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { TaskLiveProgress, type TaskLiveProgressModel } from "./TaskLiveProgress";
import { taskLiveFromApi } from "./taskLiveProgress";
import type { TaskRunRow } from "../types/api";

/**
 * Sidebar task progress: collapsed pill (label + stage + counts + bar) expands upward.
 * Only renders while a WORKING run has ``summary.live``.
 */

function MinimizedTaskPill(props: {
  model: TaskLiveProgressModel;
  isStudioGlass: boolean;
  onClick: () => void;
}) {
  const glass = props.isStudioGlass;
  const track = glass ? "bg-[#2a3340]" : "bg-slate-200";
  const fill = glass ? "bg-sky-400" : "bg-sky-600";
  const fillDone = glass ? "bg-emerald-400" : "bg-emerald-600";
  const pct = Math.max(0, Math.min(100, props.model.overallPct));
  const muted = glass ? "text-slate-400" : "text-slate-600";
  const fg = glass ? "text-slate-100" : "text-slate-900";
  const barFill = pct >= 100 ? fillDone : fill;

  return (
    <button
      type="button"
      onClick={props.onClick}
      aria-expanded={false}
      title="Show task progress"
      className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
        glass
          ? "border-[#424753]/55 bg-[#1e2430]/90 text-slate-200 hover:border-sky-500/40 hover:bg-[#252e3a]"
          : "border-[#c5d0de] bg-white/90 text-slate-700 hover:border-sky-500/50 hover:bg-white"
      }`}
    >
      <div className="flex items-start gap-2">
        <span
          className={`material-symbols-outlined mt-0.5 shrink-0 animate-spin ${glass ? "text-sky-400" : "text-sky-600"}`}
          style={{ fontSize: 16 }}
        >
          progress_activity
        </span>
        <span className="min-w-0 flex-1">
          <span className={`block font-headline text-[12px] uppercase tracking-wider ${fg}`}>
            {props.model.label}
          </span>
          {props.model.stageLabel ? (
            <span className={`mt-0.5 block font-headline text-[11px] uppercase tracking-wider ${muted}`}>
              {props.model.stageLabel}
            </span>
          ) : null}
          <span className={`mt-0.5 block tabular-nums text-[11px] ${muted}`}>
            {props.model.overallPct}% · {props.model.unitsDone}/{props.model.unitsTotal}
          </span>
          <div className={`mt-2 h-1.5 w-full overflow-hidden rounded-full ${track}`}>
            <div
              className={`h-full rounded-full transition-[width] duration-500 ease-out ${barFill}`}
              style={{ width: `${pct}%` }}
            />
          </div>
        </span>
      </div>
    </button>
  );
}

export function TaskStatusPillPrototype(props: {
  isStudioGlass: boolean;
  /** Working task runs; live model from the first WORKING run with ``live``. */
  taskRuns?: TaskRunRow[];
}) {
  const glass = props.isStudioGlass;
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  const liveModel = useMemo(() => {
    const working = (props.taskRuns || []).find(
      (run) => String(run.status || "").toUpperCase() === "WORKING" && run.live,
    );
    return taskLiveFromApi(working?.live);
  }, [props.taskRuns]);

  useEffect(() => {
    if (!liveModel) setExpanded(false);
  }, [liveModel]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  if (!liveModel) return null;

  const panelShell = glass
    ? "border-[#424753]/50 bg-[#141a24] text-slate-100"
    : "border-[#c5d0de] bg-[#eef3f8] text-slate-900";

  const goToTasks = () => {
    setExpanded(false);
    navigate("/activity/tasks");
  };

  return (
    <div className="relative w-full shrink-0 px-4 pb-2">
      {!expanded ? (
        <MinimizedTaskPill model={liveModel} isStudioGlass={glass} onClick={() => setExpanded(true)} />
      ) : (
        <div
          id={panelId}
          className={`overflow-hidden rounded-xl border p-3 transition-all duration-300 ease-out ${panelShell}`}
        >
          <div className="mb-2.5 flex items-start justify-between gap-2">
            <p className="text-[10px] font-headline uppercase tracking-widest opacity-50">In progress</p>
            <button
              type="button"
              onClick={() => setExpanded(false)}
              className={`rounded-md p-1 ${glass ? "text-slate-400 hover:bg-white/10 hover:text-white" : "text-slate-500 hover:bg-black/5 hover:text-slate-800"}`}
              aria-label="Collapse task progress"
            >
              <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                close
              </span>
            </button>
          </div>
          <TaskLiveProgress model={liveModel} density="compact" isStudioGlass={glass} />
          <button
            type="button"
            onClick={goToTasks}
            className={`mt-3 inline-flex w-full items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-[12px] font-headline uppercase tracking-wider transition-colors ${
              glass
                ? "border-[#424753]/55 text-slate-300 hover:border-sky-500/40 hover:bg-white/5 hover:text-white"
                : "border-[#c5d0de] text-slate-600 hover:border-sky-500/50 hover:bg-white hover:text-slate-900"
            }`}
          >
            Open in Tasks
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
              arrow_forward
            </span>
          </button>
        </div>
      )}
    </div>
  );
}
