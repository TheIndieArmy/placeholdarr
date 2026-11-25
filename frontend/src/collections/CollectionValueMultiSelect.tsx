import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ThemeMode } from "../brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import { getCollectionTheme } from "./collectionTheme";

function mergeUniqueSorted(...lists: (string[] | undefined)[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const list of lists) {
    for (const raw of list || []) {
      const item = String(raw).trim();
      if (!item) continue;
      const key = item.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
  }
  return out.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}

export { mergeUniqueSorted };

export function CollectionValueMultiSelect(props: {
  themeMode: ThemeMode;
  accentHex: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  hint?: string;
  searchPlaceholder?: string;
  addLabel?: string;
  emptyOptionsMessage?: string;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [panelRect, setPanelRect] = useState<{ top: number; left: number; width: number } | null>(null);

  const syncPanelPosition = useCallback(() => {
    const btn = addButtonRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    setPanelRect({
      top: rect.bottom + 4,
      left: rect.left,
      width: Math.max(rect.width, 256),
    });
  }, []);

  useEffect(() => {
    if (!open) {
      setPanelRect(null);
      return;
    }
    syncPanelPosition();
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onReposition = () => syncPanelPosition();
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("scroll", onReposition, true);
    window.addEventListener("resize", onReposition);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("scroll", onReposition, true);
      window.removeEventListener("resize", onReposition);
    };
  }, [open, syncPanelPosition]);

  const selectedLower = useMemo(() => new Set(props.selected.map((s) => s.toLowerCase())), [props.selected]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return props.options.filter((o) => {
      if (selectedLower.has(o.toLowerCase())) return false;
      if (!q) return true;
      return o.toLowerCase().includes(q);
    });
  }, [props.options, query, selectedLower]);

  const add = (value: string) => {
    const trimmed = String(value || "").trim();
    if (!trimmed || selectedLower.has(trimmed.toLowerCase())) return;
    props.onChange([...props.selected, trimmed]);
    setQuery("");
  };

  const commitSearchQuery = () => {
    const q = query.trim();
    if (!q) return;
    if (filtered.length) {
      add(filtered[0]);
      return;
    }
    add(q);
  };

  const remove = (value: string) => {
    props.onChange(props.selected.filter((v) => v.toLowerCase() !== value.toLowerCase()));
  };

  const fieldClass = `w-full rounded-md border px-2.5 py-1.5 text-[13px] outline-none focus:ring-1 focus:ring-[var(--brand-accent-3)] ${
    isLight ? "border-slate-200 bg-white text-slate-900" : "border-[#424753] bg-[#0d1118] text-slate-100"
  }`;

  return (
    <div ref={rootRef} className="mt-3 pl-8 flex flex-col gap-2 max-w-xl">
      {props.hint ? <p className={`text-[12px] ${theme.muted}`}>{props.hint}</p> : null}
      {props.selected.length ? (
        <div className="flex flex-wrap gap-1.5">
          {props.selected.map((val) => (
            <span
              key={val}
              className={`inline-flex items-center gap-1 rounded-md pl-2.5 pr-1 py-1 text-[12px] border border-transparent ${FG_ON_ACCENT_TEXT_CLASS}`}
              style={accentFilledStyle(props.accentHex)}
            >
              <span className="max-w-[14rem] truncate">{val}</span>
              <button
                type="button"
                className="rounded p-0.5 opacity-90 hover:opacity-100"
                aria-label={`Remove ${val}`}
                onClick={() => remove(val)}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                  close
                </span>
              </button>
            </span>
          ))}
        </div>
      ) : (
        <p className={`text-[12px] ${theme.muted}`}>Nothing selected yet. Use Add to pick from the list.</p>
      )}

      <div className="relative">
        <button
          ref={addButtonRef}
          type="button"
          onClick={() => {
            setOpen((v) => {
              const next = !v;
              if (next) {
                requestAnimationFrame(() => syncPanelPosition());
              }
              return next;
            });
          }}
          className={`inline-flex items-center gap-1 rounded-lg border px-3 py-1.5 text-[13px] ${
            isLight
              ? "border-slate-200 text-slate-700 hover:bg-slate-50"
              : "border-[#424753]/80 text-slate-200 hover:bg-[#1e2430]"
          }`}
        >
          <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
            add
          </span>
          {props.addLabel || "Add"}
        </button>
        {open && panelRect ? (
          <div
            className={`fixed z-[90] max-w-md rounded-lg border p-2 shadow-lg shadow-black/40 ${
              isLight ? "border-slate-200 bg-white" : "border-[#424753] bg-[#12161f]"
            }`}
            style={{
              top: panelRect.top,
              left: panelRect.left,
              width: panelRect.width,
            }}
          >
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitSearchQuery();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setOpen(false);
                }
              }}
              placeholder={props.searchPlaceholder || "Search…"}
              className={fieldClass}
              autoFocus
            />
            <ul className={`mt-2 max-h-52 overflow-y-auto text-[13px] ${isLight ? "text-slate-800" : "text-slate-200"}`}>
              {filtered.length ? (
                filtered.map((opt) => (
                  <li key={opt}>
                    <button
                      type="button"
                      className={`w-full rounded-md px-2 py-1.5 text-left ${
                        isLight ? "hover:bg-slate-100" : "hover:bg-[#1e2430]"
                      }`}
                      onClick={() => add(opt)}
                    >
                      {opt}
                    </button>
                  </li>
                ))
              ) : (
                <li className={`px-2 py-2 text-[12px] ${theme.muted}`}>
                  {props.options.length
                    ? "No matches. Try a different search."
                    : props.emptyOptionsMessage || "No options loaded yet."}
                </li>
              )}
            </ul>
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className={`text-[12px] underline ${theme.muted}`}
          onClick={() => props.onChange([...props.options])}
          disabled={!props.options.length}
        >
          Select all
        </button>
        <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => props.onChange([])}>
          Clear all
        </button>
      </div>
    </div>
  );
}