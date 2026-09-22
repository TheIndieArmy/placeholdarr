import { useEffect, useRef, useState } from "react";
import { fetchJson, getCsrfToken } from "./api/client";

interface DummyMediaInfo {
  kind: "primary" | "coming_soon";
  path: string;
  exists: boolean;
  size_bytes: number;
  modified_at: number | null;
  is_default: boolean;
}

type StatusMap = Record<"primary" | "coming_soon", DummyMediaInfo | undefined>;

const KIND_LABELS: Record<"primary" | "coming_soon", { title: string; description: string }> = {
  primary: {
    title: "Standard placeholder video",
    description: "Shown for titles added but not yet downloaded (dummy.mp4).",
  },
  coming_soon: {
    title: "Coming Soon placeholder video",
    description: "Shown for titles that have not aired/released yet (coming_soon_dummy.mp4). Falls back to the standard video if not set.",
  },
};

function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatModified(mtime: number | null): string {
  if (!mtime) return "unknown";
  try {
    return new Date(mtime * 1000).toLocaleString();
  } catch {
    return "unknown";
  }
}

async function uploadDummyMedia(kind: string, file: File): Promise<DummyMediaInfo> {
  const form = new FormData();
  form.append("file", file);
  const csrf = getCsrfToken();
  const response = await fetch(`/api/settings/dummy-media/upload?kind=${encodeURIComponent(kind)}`, {
    method: "POST",
    credentials: "same-origin",
    headers: csrf ? { "X-CSRF-Token": csrf } : undefined,
    body: form,
  });
  if (!response.ok) {
    let message = `Upload failed: ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload?.detail) message = payload.detail;
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }
  return (await response.json()) as DummyMediaInfo;
}

function DummyMediaRow(props: {
  kind: "primary" | "coming_soon";
  info: DummyMediaInfo | undefined;
  accentHex: string;
  onChanged: (kind: "primary" | "coming_soon", info: DummyMediaInfo) => void;
}) {
  const { kind, info, accentHex, onChanged } = props;
  const labels = KIND_LABELS[kind];
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleFileChosen(file: File | undefined | null) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await uploadDummyMedia(kind, file);
      onChanged(kind, updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function handleReset() {
    setBusy(true);
    setError(null);
    try {
      const updated = await fetchJson<DummyMediaInfo>(`/api/settings/dummy-media/reset?kind=${encodeURIComponent(kind)}`, {
        method: "POST",
      });
      onChanged(kind, updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 px-6 py-5 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="text-[14px] font-semibold text-white">{labels.title}</div>
        <div className="mt-0.5 text-[13px] text-white/60">{labels.description}</div>
        <div className="mt-1.5 text-[12px] text-white/40">
          {info?.exists ? (
            <>
              {info.is_default ? "Default video" : "Custom video"} · {formatBytes(info.size_bytes)} · updated {formatModified(info.modified_at)}
            </>
          ) : (
            "No file present yet."
          )}
        </div>
        {error ? <div className="mt-1.5 text-[12px] text-red-400">{error}</div> : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <input
          ref={inputRef}
          type="file"
          accept="video/mp4,video/quicktime,video/x-matroska,.mp4,.mov,.mkv,.m4v"
          className="hidden"
          onChange={(e) => handleFileChosen(e.target.files?.[0])}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          className="flex items-center gap-1.5 rounded-lg px-4 py-2 text-[13px] font-headline uppercase tracking-wider text-white/90 disabled:cursor-not-allowed disabled:opacity-50"
          style={{ backgroundColor: `${accentHex}26`, border: `1px solid ${accentHex}66` }}
        >
          <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
            upload
          </span>
          {busy ? "Uploading…" : "Upload video"}
        </button>
        {info?.exists && !info.is_default ? (
          <button
            type="button"
            disabled={busy}
            onClick={handleReset}
            className="flex items-center gap-1.5 rounded-lg border border-[#424753]/60 px-3 py-2 text-[13px] font-headline uppercase tracking-wider text-white/60 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
            title="Restore the bundled default video"
          >
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
              restart_alt
            </span>
            Reset
          </button>
        ) : null}
      </div>
    </div>
  );
}

export default function DummyMediaSettings(props: { accentHex: string }) {
  const [status, setStatus] = useState<StatusMap>({ primary: undefined, coming_soon: undefined });
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchJson<StatusMap>("/api/settings/dummy-media/status")
      .then((data) => {
        if (!cancelled) setStatus(data);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function handleChanged(kind: "primary" | "coming_soon", info: DummyMediaInfo) {
    setStatus((prev) => ({ ...prev, [kind]: info }));
  }

  return (
    <div className="mt-6 bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden">
      <div className="px-6 py-4 border-b border-[#424753]/30">
        <h2 className="text-[18px] font-bold text-white font-headline">Placeholder Videos</h2>
        <p className="mt-1 text-[13px] text-white/50">
          Replace the video files placeholders link to when a title is browsed/scrubbed before it is downloaded.
        </p>
      </div>
      {loadError ? (
        <div className="px-6 py-4 text-[13px] text-red-400">{loadError}</div>
      ) : (
        <div className="divide-y divide-[#424753]/20">
          <DummyMediaRow kind="primary" info={status.primary} accentHex={props.accentHex} onChanged={handleChanged} />
          <DummyMediaRow kind="coming_soon" info={status.coming_soon} accentHex={props.accentHex} onChanged={handleChanged} />
        </div>
      )}
    </div>
  );
}
