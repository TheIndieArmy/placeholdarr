import { useEffect, useId, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ConfirmModal } from "../ConfirmModal";
import type { ThemeMode } from "../brandTypes";
import { TaskLiveProgress, type TaskLiveProgressModel } from "./TaskLiveProgress";
import { taskLiveFromApi } from "./taskLiveProgress";
import { TASK_KEYS_WITH_STOP_SUPPORT, TASK_STOP_CONFIRM_MESSAGE } from "./taskStopConfirm";
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
  const queued = String(props.model.stage || "").toLowerCase() === "queued";
  const barFill = queued ? track : pct >= 100 ? fillDone : fill;
  const stageText = queued ? "Queued" : props.model.stageLabel;
  const countsText = queued
    ? props.model.unitsTotal > 0
      ? `0% · 0/${props.model.unitsTotal}`
      : "Waiting for a worker"
    : `${props.model.overallPct}% · ${props.model.unitsDone}/${props.model.unitsTotal}`;

  return (
    <button
      type="button"
      onClick={props.onClick}
      aria-expanded={false}
      title={queued ? "Show queued task" : "Show task progress"}
      className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
        glass
          ? "border-[#424753]/55 bg-[#1e2430]/90 text-slate-200 hover:border-sky-500/40 hover:bg-[#252e3a]"
          : "border-[#c5d0de] bg-white/90 text-slate-700 hover:border-sky-500/50 hover:bg-white"
      }`}
    >
      <div className="flex items-start gap-2">
        <span
          className={`material-symbols-outlined mt-0.5 shrink-0 ${
            queued
              ? glass
                ? "text-slate-400"
                : "text-slate-500"
              : `animate-spin ${glass ? "text-sky-400" : "text-sky-600"}`
          }`}
          style={{ fontSize: 16 }}
        >
          {queued ? "schedule" : "progress_activity"}
        </span>
        <span className="min-w-0 flex-1">
          <span className={`block font-headline text-[12px] uppercase tracking-wider ${fg}`}>
            {props.model.label}
          </span>
          {stageText ? (
            <span className={`mt-0.5 block font-headline text-[11px] uppercase tracking-wider ${muted}`}>
              {stageText}
            </span>
          ) : null}
          <span className={`mt-0.5 block tabular-nums text-[11px] ${muted}`}>{countsText}</span>
          <div className={`mt-2 h-1.5 w-full overflow-hidden rounded-full ${track}`}>
            <div
              className={`h-full rounded-full transition-[width] duration-500 ease-out ${barFill}`}
              style={{ width: queued ? "0%" : `${pct}%` }}
            />
          </div>
        </span>
      </div>
    </button>
  );
}

export function TaskStatusPillPrototype(props: {
  isStudioGlass: boolean;
  themeMode: ThemeMode;
  accentHex: string;
  /** Working task runs; live model from the first WORKING run with ``live``. */
  taskRuns?: TaskRunRow[];
  onStopRun: (runId: number) => Promise<void> | void;
}) {
  const glass = props.isStudioGlass;
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const panelId = useId();

  const workingRun = useMemo(() => {
    return (props.taskRuns || []).find(
      (run) => String(run.status || "").toUpperCase() === "WORKING" && run.live,
    );
  }, [props.taskRuns]);

  const canStop = Boolean(
    workingRun &&
      String(workingRun.trigger || "").trim().toLowerCase() !== "startup" &&
      TASK_KEYS_WITH_STOP_SUPPORT.has(String(workingRun.task_key || "").trim().toLowerCase()),
  );

  const liveModel = useMemo(() => taskLiveFromApi(workingRun?.live), [workingRun]);

  useEffect(() => {
    if (!liveModel) {
      setExpanded(false);
      setStopConfirmOpen(false);
      setStopError(null);
    }
  }, [liveModel]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  if (!liveModel || !workingRun) return null;

  const queued = String(liveModel.stage || "").toLowerCase() === "queued";
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
          className={`min-w-0 overflow-hidden rounded-xl border p-3 transition-all duration-300 ease-out ${panelShell}`}
        >
          <div className="mb-2.5 flex items-start justify-between gap-2">
            <p className="text-[10px] font-headline uppercase tracking-widest opacity-50">
              {queued ? "Queued" : "In progress"}
            </p>
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
          <TaskLiveProgress model={liveModel} density="compact" isStudioGlass={glass} showFullDetails />
          <div className="mt-3 flex flex-col gap-2">
            {canStop ? (
              <button
                type="button"
                onClick={() => {
                  setStopError(null);
                  setStopConfirmOpen(true);
                }}
                className={`inline-flex w-full items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-[12px] font-headline uppercase tracking-wider transition-colors ${
                  glass
                    ? "border-rose-500/45 text-rose-300 hover:bg-rose-500/10 hover:text-rose-200"
                    : "border-rose-400/70 text-rose-700 hover:bg-rose-50 hover:text-rose-800"
                }`}
              >
                Stop task
              </button>
            ) : null}
            <button
              type="button"
              onClick={goToTasks}
              className={`inline-flex w-full items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-[12px] font-headline uppercase tracking-wider transition-colors ${
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
        </div>
      )}
      {stopConfirmOpen ? (
        <ConfirmModal
          title="Stop this task?"
          message={TASK_STOP_CONFIRM_MESSAGE}
          error={stopError}
          confirmLabel={stopPending ? "Stopping…" : "Stop task"}
          cancelLabel="Keep running"
          accentHex={props.accentHex}
          themeMode={props.themeMode}
          onCancel={() => {
            if (stopPending) return;
            setStopConfirmOpen(false);
            setStopError(null);
          }}
          onConfirm={() => {
            if (stopPending) return;
            setStopPending(true);
            setStopError(null);
            void Promise.resolve(props.onStopRun(workingRun.id))
              .then(() => {
                setStopConfirmOpen(false);
                setStopError(null);
                setExpanded(false);
              })
              .catch((e) => {
                setStopError(e instanceof Error ? e.message : "Failed to stop task");
              })
              .finally(() => {
                setStopPending(false);
              });
          }}
        />
      ) : null}
    </div>
  );
}
