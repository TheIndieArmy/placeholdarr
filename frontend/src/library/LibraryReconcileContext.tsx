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

type LibraryReconcileSidebarStatus = {
  message: string | null;
  kind: "info" | "success" | "error";
  busy: boolean;
};

type LibraryReconcileContextValue = {
  status: LibraryReconcileSidebarStatus;
  runReconcile: (startReconcile: () => Promise<EntityReconcileStartResponse>) => Promise<void>;
};

const LibraryReconcileContext = createContext<LibraryReconcileContextValue | null>(null);

export function useLibraryReconcile(): LibraryReconcileContextValue {
  const ctx = useContext(LibraryReconcileContext);
  if (!ctx) {
    throw new Error("useLibraryReconcile must be used within LibraryReconcileProvider");
  }
  return ctx;
}

export function LibraryReconcileProvider(props: { children: ReactNode }) {
  const [status, setStatus] = useState<LibraryReconcileSidebarStatus>({
    message: null,
    kind: "info",
    busy: false,
  });
  const [pollingJobId, setPollingJobId] = useState<number | null>(null);

  useEffect(() => {
    if (status.kind !== "success" || !status.message) return;
    const clearTimer = window.setTimeout(() => {
      setStatus({ message: null, kind: "info", busy: false });
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

        if (jobStatus.status === "failed") {
          setPollingJobId(null);
          setStatus({
            busy: false,
            kind: "error",
            message: jobStatus.error_message || "Refresh failed",
          });
          return;
        }

        if (jobStatus.status === "done") {
          setPollingJobId(null);
          setStatus({
            busy: false,
            kind: "success",
            message: "Refresh complete",
          });
          return;
        }

        setStatus({
          busy: true,
          kind: "info",
          message: jobStatus.step_label || "Working…",
        });
      } catch (e) {
        if (!stopped) {
          setPollingJobId(null);
          setStatus({
            busy: false,
            kind: "error",
            message: e instanceof Error ? e.message : "Could not load refresh status",
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
    setStatus({ busy: true, kind: "info", message: "Refresh queued…" });
    try {
      const out = await startReconcile();
      if (!out.ok || out.job_id == null) {
        throw new Error(out.message || "Failed to start refresh");
      }
      setStatus({
        busy: true,
        kind: "info",
        message: out.step_label || "Refresh queued…",
      });
      setPollingJobId(out.job_id);
    } catch (e) {
      setStatus({
        busy: false,
        kind: "error",
        message: e instanceof Error ? e.message : "Failed to start refresh",
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

export function LibraryReconcileSidebarFooter(props: { isStudioGlass: boolean }) {
  const ctx = useContext(LibraryReconcileContext);
  const { message, kind, busy } = ctx?.status ?? { message: null, kind: "info" as const, busy: false };
  if (!message && !busy) return null;

  const textClass =
    kind === "error"
      ? "text-red-400"
      : kind === "success"
        ? "text-emerald-400"
        : props.isStudioGlass
          ? "text-slate-400"
          : "text-slate-600";

  return (
    <div
      className={`w-full shrink-0 border-t px-4 pt-3 pb-6 ${props.isStudioGlass ? "border-[#424753]/40 bg-[#141a24]" : "border-[#d7e2f0] bg-[#eef3f8]"}`}
      aria-live="polite"
    >
      <div className={`flex min-h-[2.75rem] items-center gap-2 text-[14px] leading-snug ${textClass}`}>
        {busy ? (
          <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 16 }}>
            progress_activity
          </span>
        ) : kind === "success" ? (
          <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>
            check_circle
          </span>
        ) : kind === "error" ? (
          <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>
            error
          </span>
        ) : null}
        <span className="line-clamp-3">{message || "Working…"}</span>
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
