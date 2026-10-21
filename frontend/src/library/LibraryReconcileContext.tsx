import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  getEntityReconcileStatus,
  type EntityReconcileStartResponse,
} from "../api/dashboard";

/** Ordered pipeline steps for the sidebar progress bar (matches entity_reconcile). */
const RECONCILE_STEPS = [
  "queued",
  "arr_refresh",
  "arr_wait_refresh",
  "sync_catalog",
  "filesystem",
  "determination",
  "materialization",
  "sidecars",
  "player",
  "done",
] as const;

type LibraryReconcileSidebarStatus = {
  message: string | null;
  kind: "info" | "success" | "error";
  busy: boolean;
  step: string | null;
  title: string | null;
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
};

export function useLibraryReconcile(): LibraryReconcileContextValue {
  const ctx = useContext(LibraryReconcileContext);
  if (!ctx) {
    throw new Error("useLibraryReconcile must be used within LibraryReconcileProvider");
  }
  return ctx;
}

function stepProgressPct(step: string | null, kind: LibraryReconcileSidebarStatus["kind"]): number {
  if (kind === "success") return 100;
  if (kind === "error") return 0;
  const key = String(step || "queued").trim().toLowerCase();
  const idx = RECONCILE_STEPS.indexOf(key as (typeof RECONCILE_STEPS)[number]);
  if (idx < 0) return 0;
  // Leave room under 100% until done so the bar does not look finished mid-run.
  return Math.round((idx / Math.max(1, RECONCILE_STEPS.length - 1)) * 100);
}

export function LibraryReconcileProvider(props: { children: ReactNode }) {
  const [status, setStatus] = useState<LibraryReconcileSidebarStatus>(IDLE_STATUS);
  const [pollingJobId, setPollingJobId] = useState<number | null>(null);

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

        const title = String(jobStatus.display_title || "").trim() || null;

        if (jobStatus.status === "failed") {
          setPollingJobId(null);
          setStatus({
            busy: false,
            kind: "error",
            message: jobStatus.error_message || "Refresh failed",
            step: jobStatus.step || null,
            title,
          });
          return;
        }

        if (jobStatus.status === "done") {
          setPollingJobId(null);
          setStatus({
            busy: false,
            kind: "success",
            message: "Refresh complete",
            step: "done",
            title,
          });
          return;
        }

        setStatus({
          busy: true,
          kind: "info",
          message: jobStatus.step_label || "Working…",
          step: jobStatus.step || null,
          title,
        });
      } catch (e) {
        if (!stopped) {
          setPollingJobId(null);
          setStatus({
            busy: false,
            kind: "error",
            message: e instanceof Error ? e.message : "Could not load refresh status",
            step: null,
            title: null,
          });
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
  }, [pollingJobId]);

  const runReconcile = useCallback(async (startReconcile: () => Promise<EntityReconcileStartResponse>) => {
    setPollingJobId(null);
    setStatus({
      busy: true,
      kind: "info",
      message: "Refresh queued…",
      step: "queued",
      title: null,
    });
    try {
      const out = await startReconcile();
      if (!out.ok || out.job_id == null) {
        throw new Error(out.message || "Failed to start refresh");
      }
      setStatus({
        busy: true,
        kind: "info",
        message: out.step_label || "Refresh queued…",
        step: out.step || "queued",
        title: String(out.display_title || "").trim() || null,
      });
      setPollingJobId(out.job_id);
    } catch (e) {
      setStatus({
        busy: false,
        kind: "error",
        message: e instanceof Error ? e.message : "Failed to start refresh",
        step: null,
        title: null,
      });
    }
  }, []);

  const value = useMemo(() => ({ status, runReconcile }), [status, runReconcile]);

  return (
    <LibraryReconcileContext.Provider value={value}>
      {props.children}
    </LibraryReconcileContext.Provider>
  );
}

export function LibraryReconcileSidebarFooter(props: {
  isStudioGlass: boolean;
  /** When a task live-progress pill is active, hide reconcile chrome so they do not stack. */
  yieldToTaskLive?: boolean;
}) {
  const ctx = useContext(LibraryReconcileContext);
  const status = ctx?.status ?? IDLE_STATUS;
  const { message, kind, busy, step, title } = status;
  if (props.yieldToTaskLive) return null;
  if (!message && !busy) return null;

  const glass = props.isStudioGlass;
  const queued = busy && String(step || "").toLowerCase() === "queued";
  const pct = stepProgressPct(step, kind);
  const track = glass ? "bg-[#2a3340]" : "bg-slate-200";
  const fill = glass ? "bg-sky-400" : "bg-sky-600";
  const fillDone = glass ? "bg-emerald-400" : "bg-emerald-600";
  const fillError = glass ? "bg-rose-400" : "bg-rose-500";
  const muted = glass ? "text-slate-400" : "text-slate-600";
  const fg = glass ? "text-slate-100" : "text-slate-900";
  const barFill =
    kind === "error" ? fillError : kind === "success" || pct >= 100 ? fillDone : queued ? track : fill;
  const label = title || "Title refresh";
  const stageText =
    kind === "success" ? "Done" : kind === "error" ? "Failed" : queued ? "Queued" : message || "Working…";
  const countsText =
    kind === "success"
      ? "100%"
      : kind === "error"
        ? message || "Failed"
        : queued
          ? "Waiting for a worker"
          : `${pct}%`;

  return (
    <div className="relative w-full shrink-0 px-4 pb-2" aria-live="polite">
      <div
        className={`w-full rounded-lg border px-3 py-2.5 text-left ${
          glass
            ? "border-[#424753]/55 bg-[#1e2430]/90 text-slate-200"
            : "border-[#c5d0de] bg-white/90 text-slate-700"
        }`}
      >
        <div className="flex items-start gap-2">
          <span
            className={`material-symbols-outlined mt-0.5 shrink-0 ${
              kind === "success"
                ? glass
                  ? "text-emerald-400"
                  : "text-emerald-600"
                : kind === "error"
                  ? glass
                    ? "text-rose-400"
                    : "text-rose-600"
                  : queued
                    ? glass
                      ? "text-slate-400"
                      : "text-slate-500"
                    : `animate-spin ${glass ? "text-sky-400" : "text-sky-600"}`
            }`}
            style={{ fontSize: 16 }}
          >
            {kind === "success"
              ? "check_circle"
              : kind === "error"
                ? "error"
                : queued
                  ? "schedule"
                  : "progress_activity"}
          </span>
          <span className="min-w-0 flex-1">
            <span className={`block font-headline text-[12px] uppercase tracking-wider ${fg}`}>
              {label}
            </span>
            <span className={`mt-0.5 block font-headline text-[11px] uppercase tracking-wider ${muted} line-clamp-2`}>
              {stageText}
            </span>
            <span className={`mt-0.5 block tabular-nums text-[11px] ${muted} line-clamp-2`}>
              {countsText}
            </span>
            <div className={`mt-2 h-1.5 w-full overflow-hidden rounded-full ${track}`}>
              <div
                className={`h-full rounded-full transition-[width] duration-500 ease-out ${barFill}`}
                style={{ width: queued ? "0%" : `${Math.max(0, Math.min(100, pct))}%` }}
              />
            </div>
          </span>
        </div>
      </div>
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
