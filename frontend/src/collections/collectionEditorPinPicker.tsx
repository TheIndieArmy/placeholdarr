import { useEffect, useMemo, useRef, useState } from "react";
import type { CollectionPinnedItem, LibraryItem } from "../types/api";
import { useCollectionTheme } from "./collectionTheme";

export function pinKey(item: { tmdb_id?: number | null; tvdb_id?: number | null; imdb_id?: string | null }): string {
  return `${item.tmdb_id ?? ""}:${item.tvdb_id ?? ""}:${item.imdb_id ?? ""}`;
}

export function PinPicker(props: {
  items: CollectionPinnedItem[];
  accentHex: string;
  catalog: LibraryItem[];
  catalogLoading: boolean;
  placeholder: string;
  onAdd: (item: CollectionPinnedItem) => void;
  onRemove: (index: number) => void;
}) {
  const theme = useCollectionTheme();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    return props.catalog
      .filter((item) => item.title.toLowerCase().includes(needle))
      .sort((left, right) => {
        const leftStarts = left.title.toLowerCase().startsWith(needle) ? 0 : 1;
        const rightStarts = right.title.toLowerCase().startsWith(needle) ? 0 : 1;
        if (leftStarts !== rightStarts) return leftStarts - rightStarts;
        return left.title.localeCompare(right.title);
      })
      .slice(0, 10);
  }, [props.catalog, query]);

  const pinnedKeys = new Set(props.items.map(pinKey));

  return (
    <div className="flex flex-col gap-2.5">
      <div className="relative" ref={ref}>
        <div className="flex items-center gap-2">
          <span className={`material-symbols-outlined ${theme.iconMuted}`} style={{ fontSize: 17 }}>
            search
          </span>
          <input
            className={`${theme.field} flex-1`}
            value={query}
            placeholder={props.placeholder}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
          />
          {props.catalogLoading ? (
            <span className="h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: props.accentHex }} />
          ) : null}
        </div>
        {open && query.trim() && !results.length && !props.catalogLoading ? (
          <div className={theme.dropdownEmpty}>No catalog titles match.</div>
        ) : null}
        {open && results.length ? (
          <div className={`${theme.dropdown} max-h-72 overflow-y-auto`}>
            {results.map((item) => {
              const candidate: CollectionPinnedItem = {
                tmdb_id: item.tmdb_id ?? null,
                tvdb_id: item.tvdb_id ?? null,
                imdb_id: item.imdb_id ?? null,
                title: item.title,
                year: item.year ?? null,
                poster: item.poster_url ?? null,
              };
              const already = pinnedKeys.has(pinKey(candidate));
              return (
                <button
                  key={item.id}
                  type="button"
                  disabled={already}
                  onClick={() => {
                    props.onAdd(candidate);
                    setQuery("");
                    setOpen(false);
                  }}
                  className={`${theme.dropdownItem} items-center gap-2.5 px-3 py-2`}
                >
                  {item.poster_url ? (
                    <img src={item.poster_url} alt="" className={`h-10 w-7 rounded object-cover ${theme.posterFallback}`} />
                  ) : (
                    <span
                      className={`flex h-10 w-7 items-center justify-center rounded material-symbols-outlined ${theme.posterFallback}`}
                      style={{ fontSize: 14 }}
                    >
                      movie
                    </span>
                  )}
                  <span className="flex-1 min-w-0">
                    <span className={theme.dropdownItemTitle}>{item.title}</span>
                    <span className={theme.dropdownItemDescription}>{item.year ?? "—"}</span>
                  </span>
                  {already ? <span className={`text-[11px] uppercase ${theme.muted}`}>pinned</span> : null}
                </button>
              );
            })}
          </div>
        ) : null}
      </div>
      {props.items.length ? (
        <div className="flex flex-wrap gap-2">
          {props.items.map((item, index) => (
            <span key={pinKey(item)} className={theme.pinTag}>
              {item.poster ? (
                <img src={item.poster} alt="" className="h-8 w-[1.35rem] rounded-sm object-cover" />
              ) : null}
              <span className={theme.pinTitle}>
                {item.title}
                {item.year ? <span className={theme.pinYear}> ({item.year})</span> : null}
              </span>
              <button
                type="button"
                onClick={() => props.onRemove(index)}
                className={`material-symbols-outlined transition-colors ${theme.iconAction}`}
                style={{ fontSize: 15 }}
                title="Remove pin"
              >
                close
              </button>
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}