import { useEffect, useRef, useState } from "react";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";
import type { ThemeMode } from "../../brandTypes";
import { getCollectionTheme } from "../collectionTheme";

type AddKind = "recipe" | "set" | "discover" | "import";

const MENU: { kind: Exclude<AddKind, "recipe">; label: string; description: string; icon: string }[] = [
  { kind: "set", label: "Collection set", description: "One collection per genre, studio, network, or tag.", icon: "category" },
  { kind: "discover", label: "Discover setup", description: "Trending, popular, upcoming, and genre rows.", icon: "explore" },
  { kind: "import", label: "Import from JSON", description: "Restore or copy recipes from an export.", icon: "upload" },
];

/** Primary "New collection" plus a menu for sets, Discover, and import. */
export function SplitAddButton(props: {
  themeMode: ThemeMode;
  accentHex: string;
  disabled: boolean;
  ioBusy: boolean;
  onPick: (kind: AddKind) => void;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const base = `flex items-center gap-1.5 py-2 text-[14px] font-headline uppercase tracking-wider disabled:cursor-not-allowed disabled:opacity-40 ${FG_ON_ACCENT_TEXT_CLASS}`;

  return (
    <div ref={wrapRef} className="relative flex">
      <button
        type="button"
        disabled={props.disabled}
        title={props.disabled ? "Configure Plex first" : "Create a rule-based collection"}
        onClick={() => props.onPick("recipe")}
        className={`${base} rounded-l-lg pl-4 pr-3`}
        style={accentFilledStyle(props.accentHex)}
      >
        <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
          add
        </span>
        New collection
      </button>
      <button
        type="button"
        disabled={props.disabled}
        aria-label="More ways to add"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={`${base} rounded-r-lg border-l border-black/20 px-2`}
        style={accentFilledStyle(props.accentHex)}
      >
        <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
          expand_more
        </span>
      </button>
      {open ? (
        <div
          className={`absolute right-0 top-full z-50 mt-1 w-72 overflow-hidden rounded-lg border shadow-lg ${
            isLight ? "border-slate-200 bg-white" : "border-[#424753]/60 bg-[#171c22]"
          }`}
        >
          {MENU.map((item) => {
            const disabled = item.kind !== "set" && props.ioBusy;
            return (
              <button
                key={item.kind}
                type="button"
                disabled={disabled}
                onClick={() => {
                  setOpen(false);
                  props.onPick(item.kind);
                }}
                className={`flex w-full items-start gap-3 px-3 py-2.5 text-left disabled:opacity-40 ${
                  isLight ? "hover:bg-slate-50" : "hover:bg-[#1e2430]"
                }`}
              >
                <span className="material-symbols-outlined mt-0.5" style={{ fontSize: 18, color: props.accentHex }}>
                  {item.icon}
                </span>
                <span className="min-w-0">
                  <span className={`block text-[14px] font-semibold ${isLight ? "text-slate-900" : "text-slate-100"}`}>
                    {item.label}
                  </span>
                  <span className={`block text-[12px] ${theme.muted}`}>{item.description}</span>
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
