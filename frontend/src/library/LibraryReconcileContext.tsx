import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  getEntityReconcileStatus,
  type EntityReconcileStartResponse,
} from "../api/dashboard";
import { TaskLiveProgress } from "../activity/TaskLiveProgress";
import { taskLiveFromApi } from "../activity/taskLiveProgress";
import type { TaskRunLiveProgress } from "../types/api";

type LibraryReconcileSidebarStatus = {
  message: string | null;
  kind: "info" | "success" | "error";
  busy: boolean;
  step: string | null;
  title: string | null;
  live: TaskRunLiveProgress | null;
};

type LibraryReconcileContextValue = {
  status: LibraryReconcileSidebarStatus;
  runReconcile: (startReconcile: () => Promise<EntityReconcileStartResponse>) => Promise<void>;
};

const LibraryReconcileContext = createContext<LibraryReconcileContextValue | null>(null);

const IDLE_STATUS: LibraryReconcileSidebarStatus = {
  message: null,
  kind: "info",
  busy: false,
  step: null,
  title: null,
  live: null,
};

/** Survives full page reloads so the sidebar pill can resume polling a working job. */
const RECONCILE_JOB_SESSION_KEY = "placeholdarr.libraryReconcileJobId";

type StoredReconcileJobs = {
  jobId: number;
  followupJobId: number | null;
};

function _positiveJobId(raw: unknown): number | null {
  const id = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(id) && id > 0 ? Math.trunc(id) : null;
}

function readStoredReconcileJobs(): StoredReconcileJobs | null {
  try {
    const raw = sessionStorage.getItem(RECONCILE_JOB_SESSION_KEY);
    if (!raw) return null;
    // Legacy: plain numeric job id string.
    if (/^\d+$/.test(raw.trim())) {
      const jobId = _positiveJobId(raw.trim());
      return jobId != null ? { jobId, followupJobId: null } : null;
    }
    const parsed = JSON.parse(raw) as { jobId?: unknown; followupJobId?: unknown };
    const jobId = _positiveJobId(parsed?.jobId);
    if (jobId == null) return null;
    const followupJobId = _positiveJobId(parsed?.followupJobId);
    return {
      jobId,
      followupJobId: followupJobId != null && followupJobId !== jobId ? followupJobId : null,
    };
  } catch {
    return null;
  }
}

function writeStoredReconcileJobs(jobId: number | null, followupJobId: number | null = null): void {
  try {
    if (jobId == null) {
      sessionStorage.removeItem(RECONCILE_JOB_SESSION_KEY);
      return;
    }
    const followup =
      followupJobId != null && followupJobId > 0 && followupJobId !== jobId ? followupJobId : null;
    sessionStorage.setItem(
      RECONCILE_JOB_SESSION_KEY,
      JSON.stringify({ jobId, followupJobId: followup }),
    );
  } catch {
    // Ignore quota / private-mode failures; pill just will not survive reload.
  }
}

export function useLibraryReconcile(): LibraryReconcileContextValue {
  const ctx = useContext(LibraryReconcileContext);
  if (!ctx) {
    throw new Error("useLibraryReconcile must be used within LibraryReconcileProvider");
  }
  return ctx;
}

export function LibraryReconcileProvider(props: { children: ReactNode }) {
  const [status, setStatus] = useState<LibraryReconcileSidebarStatus>(IDLE_STATUS);
  const [pollingJobId, setPollingJobId] = useState<number | null>(null);
  const [followupJobId, setFollowupJobId] = useState<number | null>(null);
  const [resumeChecked, setResumeChecked] = useState(false);
  const pollFailuresRef = useRef(0);

  const clearPollingJob = useCallback(() => {
    writeStoredReconcileJobs(null);
    setPollingJobId(null);
    setFollowupJobId(null);
    pollFailuresRef.current = 0;
  }, []);

  const beginPollingJob = useCallback((jobId: number, followupId: number | null = null) => {
    const followup = followupId != null && followupId > 0 && followupId !== jobId ? followupId : null;
    writeStoredReconcileJobs(jobId, followup);
    setPollingJobId(jobId);
    setFollowupJobId(followup);
    pollFailuresRef.current = 0;
  }, []);

  // Resume an in-flight title refresh after a full page reload.
  useEffect(() => {
    const stored = readStoredReconcileJobs();
    if (stored == null) {
      setResumeChecked(true);
      return;
    }
    setStatus({
      busy: true,
      kind: "info",
      message: "Refreshing…",
      step: "queued",
      title: null,
      live: null,
    });
    beginPollingJob(stored.jobId, stored.followupJobId);
    setResumeChecked(true);
  }, [beginPollingJob]);

  useEffect(() => {
    if (status.kind !== "success" || !status.message) return;
    const clearTimer = window.setTimeout(() => {
      setStatus(IDLE_STATUS);
    }, 3000);
    return () => window.clearTimeout(clearTimer);
  }, [status.kind, status.message]);

  useEffect(() => {
    if (pollingJobId == null) return;

    let stopped = false;

    const poll = async () => {
      try {
        const jobStatus = await getEntityReconcileStatus(pollingJobId);
        if (stopped) return;
        pollFailuresRef.current = 0;

        const title = String(jobStatus.display_title || "").trim() || null;
        const live = jobStatus.live ?? null;

        if (jobStatus.status === "failed") {
          clearPollingJob();
          setStatus({
            busy: false,
            kind: "error",
            message: jobStatus.error_message || "Refresh failed",
            step: jobStatus.step || null,
            title,
            live,
          });
          return;
        }

        if (jobStatus.status === "done") {
          if (followupJobId != null && followupJobId !== pollingJobId) {
            const nextId = followupJobId;
            setStatus({
              busy: true,
              kind: "info",
              message: "Follow-up refresh queued…",
              step: "queued",
              title,
              live: null,
            });
            beginPollingJob(nextId, null);
            return;
          }
          clearPollingJob();
          setStatus({
            busy: false,
            kind: "success",
            message: "Refresh complete",
            step: "done",
            title,
            live,
          });
          return;
        }

        setStatus({
          busy: true,
          kind: "info",
          message: jobStatus.step_label || "Working…",
          step: jobStatus.step || null,
          title,
          live,
        });
      } catch (e) {
        if (!stopped) {
          const msg = e instanceof Error ? e.message : "Could not load refresh status";
          const stale =
            /not found/i.test(msg) || /404/.test(msg) || /Reconcile job not found/i.test(msg);
          if (stale) {
            clearPollingJob();
            if (resumeChecked) {
              setStatus(IDLE_STATUS);
              return;
            }
            setStatus({
              busy: false,
              kind: "error",
              message: msg,
              step: null,
              title: null,
              live: null,
            });
            return;
          }
          // Transient network/5xx: keep polling; only fail after several misses.
          pollFailuresRef.current += 1;
          if (pollFailuresRef.current >= 3) {
            clearPollingJob();
            setStatus({
              busy: false,
              kind: "error",
              message: msg,
              step: null,
              title: null,
              live: null,
            });
          }
        }
      }
    };

    void poll();
    const interval = window.setInterval(() => {
      void poll();
    }, 1500);
    return () => {
      stopped = true;
      window.clearInterval(interval);
    };
  }, [pollingJobId, followupJobId, clearPollingJob, beginPollingJob, resumeChecked]);

  const runReconcile = useCallback(async (startReconcile: () => Promise<EntityReconcileStartResponse>) => {
    clearPollingJob();
    setStatus({
      busy: true,
      kind: "info",
      message: "Refresh queued…",
      step: "queued",
      title: null,
      live: null,
    });
    try {
      const out = await startReconcile();
      if (!out.ok || out.job_id == null) {
        throw new Error(out.message || "Failed to start refresh");
      }
      const followup =
        out.followup_job_id != null && Number(out.followup_job_id) > 0
          ? Math.trunc(Number(out.followup_job_id))
          : null;
      setStatus({
        busy: true,
        kind: "info",
        message: out.step_label || "Refresh queued…",
        step: out.step || "queued",
        title: String(out.display_title || "").trim() || null,
        live: out.live ?? null,
      });
      beginPollingJob(out.job_id, followup);
    } catch (e) {
      clearPollingJob();
      setStatus({
        busy: false,
        kind: "error",
        message: e instanceof Error ? e.message : "Failed to start refresh",
        step: null,
        title: null,
        live: null,
      });
    }
  }, [beginPollingJob, clearPollingJob]);

  const value = useMemo(() => ({ status, runReconcile }), [status, runReconcile]);

  return (
    <LibraryReconcileContext.Provider value={value}>
      {props.children}
    </LibraryReconcileContext.Provider>
  );
}

function MinimizedReconcilePill(props: {
  isStudioGlass: boolean;
  label: string;
  stageText: string;
  countsText: string;
  pct: number;
  queued: boolean;
  kind: LibraryReconcileSidebarStatus["kind"];
  onExpand?: () => void;
}) {
  const glass = props.isStudioGlass;
  const track = glass ? "bg-[#2a3340]" : "bg-slate-200";
  const fill = glass ? "bg-sky-400" : "bg-sky-600";
  const fillDone = glass ? "bg-emerald-400" : "bg-emerald-600";
  const fillError = glass ? "bg-rose-400" : "bg-rose-500";
  const muted = glass ? "text-slate-400" : "text-slate-600";
  const fg = glass ? "text-slate-100" : "text-slate-900";
  const barFill =
    props.kind === "error"
      ? fillError
      : props.kind === "success" || props.pct >= 100
        ? fillDone
        : props.queued
          ? track
          : fill;
  const shell = `w-full rounded-lg border px-3 py-2.5 text-left ${
    glass
      ? "border-[#424753]/55 bg-[#1e2430]/90 text-slate-200"
      : "border-[#c5d0de] bg-white/90 text-slate-700"
  }${
    props.onExpand
      ? glass
        ? " transition-colors hover:border-sky-500/40 hover:bg-[#252e3a]"
        : " transition-colors hover:border-sky-500/50 hover:bg-white"
      : ""
  }`;

  const body = (
    <div className="flex items-start gap-2">
      <span
        className={`material-symbols-outlined mt-0.5 shrink-0 ${
          props.kind === "success"
            ? glass
              ? "text-emerald-400"
              : "text-emerald-600"
            : props.kind === "error"
              ? glass
                ? "text-rose-400"
                : "text-rose-600"
              : props.queued
                ? glass
                  ? "text-slate-400"
                  : "text-slate-500"
                : `animate-spin ${glass ? "text-sky-400" : "text-sky-600"}`
        }`}
        style={{ fontSize: 16 }}
      >
        {props.kind === "success"
          ? "check_circle"
          : props.kind === "error"
            ? "error"
            : props.queued
              ? "schedule"
              : "progress_activity"}
      </span>
      <span className="min-w-0 flex-1">
        <span className={`block font-headline text-[12px] uppercase tracking-wider ${fg}`}>
          {props.label}
        </span>
        <span className={`mt-0.5 block font-headline text-[11px] uppercase tracking-wider ${muted} line-clamp-2`}>
          {props.stageText}
        </span>
        <span className={`mt-0.5 block tabular-nums text-[11px] ${muted} line-clamp-2`}>
          {props.countsText}
        </span>
        <div className={`mt-2 h-1.5 w-full overflow-hidden rounded-full ${track}`}>
          <div
            className={`h-full rounded-full transition-[width] duration-500 ease-out ${barFill}`}
            style={{ width: props.queued ? "0%" : `${Math.max(0, Math.min(100, props.pct))}%` }}
          />
        </div>
      </span>
    </div>
  );

  if (props.onExpand) {
    return (
      <button type="button" onClick={props.onExpand} aria-expanded={false} className={shell}>
        {body}
      </button>
    );
  }
  return <div className={shell}>{body}</div>;
}

export function LibraryReconcileSidebarFooter(props: {
  isStudioGlass: boolean;
  /** When a task live-progress pill is active, hide reconcile chrome so they do not stack. */
  yieldToTaskLive?: boolean;
}) {
  const ctx = useContext(LibraryReconcileContext);
  const status = ctx?.status ?? IDLE_STATUS;
  const { message, kind, busy, step, title, live } = status;
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  const liveModel = useMemo(() => {
    if (!live) return null;
    const model = taskLiveFromApi(live);
    if (!model) return null;
    // Prefer display title from status when live label is generic.
    if (title && (!model.label || model.label === "Title refresh")) {
      return { ...model, label: title };
    }
    return model;
  }, [live, title]);

  useEffect(() => {
    if (!busy && kind !== "success") setExpanded(false);
  }, [busy, kind]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  if (props.yieldToTaskLive) return null;
  if (!message && !busy) return null;

  const glass = props.isStudioGlass;
  const queued = busy && String(step || "").toLowerCase() === "queued";
  const pct = liveModel
    ? liveModel.overallPct
    : kind === "success"
      ? 100
      : kind === "error"
        ? 0
        : 0;
  const label = title || liveModel?.label || "Title refresh";
  const stageText =
    kind === "success"
      ? "Done"
      : kind === "error"
        ? "Failed"
        : queued
          ? "Queued"
          : liveModel?.stageLabel || message || "Working…";
  const countsText =
    kind === "success"
      ? "100%"
      : kind === "error"
        ? message || "Failed"
        : queued
          ? "Waiting for a worker"
          : liveModel
            ? `${liveModel.overallPct}% · ${liveModel.unitsDone}/${liveModel.unitsTotal}`
            : message || "Working…";

  const canExpand = Boolean(liveModel?.phases?.length) && (busy || kind === "success");

  return (
    <div className="relative w-full shrink-0 px-4 pb-2" aria-live="polite">
      {!expanded || !canExpand || !liveModel ? (
        <MinimizedReconcilePill
          isStudioGlass={glass}
          label={label}
          stageText={stageText}
          countsText={countsText}
          pct={pct}
          queued={queued}
          kind={kind}
          onExpand={canExpand ? () => setExpanded(true) : undefined}
        />
      ) : (
        <div
          id={panelId}
          className={`min-w-0 overflow-hidden rounded-xl border p-3 transition-all duration-300 ease-out ${
            glass
              ? "border-[#424753]/50 bg-[#141a24] text-slate-100"
              : "border-[#c5d0de] bg-[#eef3f8] text-slate-900"
          }`}
        >
          <div className="mb-2.5 flex items-start justify-between gap-2">
            <p className="text-[10px] font-headline uppercase tracking-widest opacity-50">
              {queued ? "Queued" : kind === "success" ? "Complete" : "In progress"}
            </p>
            <button
              type="button"
              onClick={() => setExpanded(false)}
              className={`rounded-md p-1 ${
                glass
                  ? "text-slate-400 hover:bg-white/10 hover:text-white"
                  : "text-slate-500 hover:bg-black/5 hover:text-slate-800"
              }`}
              aria-label="Collapse title refresh progress"
            >
              <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                close
              </span>
            </button>
          </div>
          <TaskLiveProgress model={liveModel} density="compact" isStudioGlass={glass} showFullDetails />
        </div>
      )}
    </div>
  );
}

export function LibraryReconcileControl(props: {
  label: string;
  startReconcile: () => Promise<EntityReconcileStartResponse>;
  buttonClassName?: string;
}) {
  const { status, runReconcile } = useLibraryReconcile();

  return (
    <button
      type="button"
      disabled={status.busy}
      onClick={() => {
        void runReconcile(props.startReconcile);
      }}
      className={
        props.buttonClassName
          ?? "px-3 py-2 rounded-lg border border-[#424753]/50 text-[12px] uppercase tracking-wider text-slate-200 hover:bg-[#252e3a] disabled:opacity-50"
      }
    >
      {props.label}
    </button>
  );
}
