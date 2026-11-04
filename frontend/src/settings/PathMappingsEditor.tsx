import { useEffect, useRef, useState } from "react";

export type PathMappingRow = {
  remote: string;
  local: string;
};

function normalizePath(path: string): string {
  let text = String(path || "")
    .trim()
    .replace(/\\/g, "/");
  while (text.includes("//")) {
    text = text.replace(/\/\//g, "/");
  }
  if (text.length > 1 && text.endsWith("/") && !(text.length === 3 && text[1] === ":")) {
    text = text.replace(/\/+$/, "");
  }
  return text;
}

function parseMappingsJson(raw: string): PathMappingRow[] {
  const text = String(raw || "").trim();
  if (!text) return [];
  try {
    const payload = JSON.parse(text);
    if (!Array.isArray(payload)) return [];
    const out: PathMappingRow[] = [];
    for (const item of payload) {
      if (!item || typeof item !== "object") continue;
      const remote = normalizePath(
        String((item as PathMappingRow).remote || (item as { host?: string }).host || ""),
      );
      const local = normalizePath(
        String((item as PathMappingRow).local || (item as { container?: string }).container || ""),
      );
      if (!remote || !local) continue;
      out.push({ remote, local });
    }
    return out;
  } catch {
    return [];
  }
}

/** Persist only complete rows; draft/empty rows stay in local React state while editing. */
function serializeMappings(rows: PathMappingRow[]): string {
  const cleaned = rows
    .map((row) => ({
      remote: normalizePath(row.remote),
      local: normalizePath(row.local),
    }))
    .filter((row) => row.remote && row.local);
  if (!cleaned.length) return "";
  return JSON.stringify(cleaned);
}

function rowsFromValue(raw: string): PathMappingRow[] {
  const parsed = parseMappingsJson(raw);
  return parsed.length ? parsed : [{ remote: "", local: "" }];
}

export function PathMappingsEditor(props: {
  value: string;
  onChange: (json: string) => void;
  focusClass: string;
  layout?: "settings" | "wizard";
}) {
  const [rows, setRows] = useState<PathMappingRow[]>(() => rowsFromValue(props.value));
  const lastEmittedRef = useRef(serializeMappings(rowsFromValue(props.value)));

  useEffect(() => {
    const incoming = String(props.value || "").trim();
    if (incoming === lastEmittedRef.current) return;
    const next = rowsFromValue(props.value);
    setRows(next);
    lastEmittedRef.current = serializeMappings(next);
  }, [props.value]);

  function commit(next: PathMappingRow[]) {
    setRows(next);
    const serialized = serializeMappings(next);
    lastEmittedRef.current = serialized;
    props.onChange(serialized);
  }

  function updateRow(index: number, patch: Partial<PathMappingRow>) {
    commit(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function addRow() {
    commit([...rows, { remote: "", local: "" }]);
  }

  function removeRow(index: number) {
    const next = rows.filter((_, i) => i !== index);
    commit(next.length ? next : [{ remote: "", local: "" }]);
  }

  const inputClass = `w-full bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-[14px] text-slate-200 placeholder-slate-600 outline-none transition-colors ${props.focusClass}`;

  return (
    <div className="space-y-3">
      <p className="ui-field-description leading-relaxed">
        When your player reports different paths than Placeholdarr (for example Windows Plex{" "}
        <code className="text-slate-300">K:\...\media</code> vs Docker{" "}
        <code className="text-slate-300">/media</code>), map the remote prefix to the local one.
        Longest match wins.
      </p>
      <div className="space-y-2">
        {rows.map((row, index) => (
          <div
            key={`path-map-${index}`}
            className="grid grid-cols-1 gap-2 rounded-xl border border-[#424753]/40 bg-[#0b111b] p-3 sm:grid-cols-[1fr_auto_1fr_auto] sm:items-center"
          >
            <input
              className={inputClass}
              value={row.remote}
              placeholder="Remote (player) path prefix"
              onChange={(e) => updateRow(index, { remote: e.target.value })}
              aria-label={`Remote path prefix ${index + 1}`}
            />
            <span className="hidden text-center text-[12px] font-headline uppercase tracking-wider text-slate-500 sm:block">
              →
            </span>
            <input
              className={inputClass}
              value={row.local}
              placeholder="Local (Placeholdarr) path prefix"
              onChange={(e) => updateRow(index, { local: e.target.value })}
              aria-label={`Local path prefix ${index + 1}`}
            />
            <button
              type="button"
              onClick={() => removeRow(index)}
              className="rounded-lg border border-[#424753]/50 px-2.5 py-2 text-[12px] font-headline uppercase tracking-wider text-slate-400 transition hover:border-red-400/50 hover:text-red-300"
              aria-label={`Remove mapping ${index + 1}`}
            >
              Remove
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        onClick={addRow}
        className="rounded-lg border border-[#424753]/55 bg-[#0f1419] px-3 py-2 text-[13px] font-headline uppercase tracking-wider text-slate-200 transition hover:bg-[#151b24]"
      >
        Add mapping
      </button>
    </div>
  );
}
