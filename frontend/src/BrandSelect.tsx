import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const MENU_OPTION_PAD_PX = 24; // px-3 left + right
const MENU_CHECK_RESERVE_PX = 28; // ml-2 + 16px icon

export type BrandSelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

const SIZE_CLASS: Record<"sm" | "md", string> = {
  sm: "pl-2 pr-7 py-1 text-[12px] min-h-[1.75rem]",
  md: "pl-2.5 pr-8 py-1.5 text-[14px] min-h-[2.25rem]",
};

const MENU_MAX_HEIGHT_PX = 240; // matches max-h-60

type MenuPosition = {
  left: number;
  width: number;
  maxHeight: number;
  openUpward: boolean;
  anchorTop: number;
  anchorBottom: number;
};

function measureMenuPosition(trigger: HTMLElement): MenuPosition {
  const rect = trigger.getBoundingClientRect();
  const spaceBelow = window.innerHeight - rect.bottom - 8;
  const spaceAbove = rect.top - 8;
  const openUpward = spaceBelow < MENU_MAX_HEIGHT_PX && spaceAbove > spaceBelow;
  const maxHeight = Math.min(MENU_MAX_HEIGHT_PX, Math.max(120, openUpward ? spaceAbove : spaceBelow));
  return {
    left: rect.left,
    width: rect.width,
    maxHeight,
    openUpward,
    anchorTop: rect.top,
    anchorBottom: rect.bottom,
  };
}

/** Branded single-select: button trigger + panel (not a native browser select). */
export function BrandSelect(props: {
  value: string;
  options: BrandSelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  size?: "sm" | "md";
  fullWidth?: boolean;
  className?: string;
  triggerClassName?: string;
  ariaLabel?: string;
  id?: string;
}) {
  const size = props.size ?? "md";
  const listId = useId();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const [menuPos, setMenuPos] = useState<MenuPosition | null>(null);
  const [panelWidth, setPanelWidth] = useState<number | null>(null);
  const measureRef = useRef<HTMLDivElement | null>(null);

  const selected = props.options.find((o) => o.value === props.value);
  const label = selected?.label ?? "";
  const optionTextClass = size === "sm" ? "text-[12px]" : "text-[14px]";

  useLayoutEffect(() => {
    if (props.fullWidth) {
      setPanelWidth(null);
      return;
    }
    const el = measureRef.current;
    if (!el) return;
    let max = 0;
    el.querySelectorAll<HTMLElement>("[data-brand-select-measure]").forEach((row) => {
      max = Math.max(max, row.offsetWidth);
    });
    if (max > 0) {
      setPanelWidth(Math.min(max, 28 * 16));
    }
  }, [optionTextClass, props.fullWidth, props.options]);

  const updateMenuPosition = () => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    setMenuPos(measureMenuPosition(trigger));
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const root = rootRef.current;
      const menu = document.getElementById(listId);
      const target = e.target as Node;
      if (root?.contains(target) || menu?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [listId, open]);

  useEffect(() => {
    if (!open) return;
    const idx = props.options.findIndex((o) => o.value === props.value);
    setHighlight(idx >= 0 ? idx : 0);
  }, [open, props.options, props.value]);

  useLayoutEffect(() => {
    if (!open) {
      setMenuPos(null);
      return;
    }
    updateMenuPosition();
    const onReflow = () => updateMenuPosition();
    window.addEventListener("resize", onReflow);
    window.addEventListener("scroll", onReflow, true);
    return () => {
      window.removeEventListener("resize", onReflow);
      window.removeEventListener("scroll", onReflow, true);
    };
  }, [open]);

  const commit = (opt: BrandSelectOption) => {
    if (opt.disabled) return;
    props.onChange(opt.value);
    setOpen(false);
  };

  const moveHighlight = (delta: number) => {
    if (!props.options.length) return;
    let next = highlight;
    for (let i = 0; i < props.options.length; i += 1) {
      next = (next + delta + props.options.length) % props.options.length;
      if (!props.options[next]?.disabled) {
        setHighlight(next);
        return;
      }
    }
  };

  const menu =
    open && menuPos ? (
      <ul
        id={listId}
        role="listbox"
        aria-label={props.ariaLabel}
        style={{
          position: "fixed",
          left: menuPos.left,
          width: panelWidth ?? menuPos.width,
          maxHeight: menuPos.maxHeight,
          zIndex: 10000,
          ...(menuPos.openUpward
            ? { bottom: window.innerHeight - menuPos.anchorTop + 4 }
            : { top: menuPos.anchorBottom + 4 }),
        }}
        className="max-h-60 overflow-y-auto rounded-xl border border-[#424753]/60 bg-[#11161d] py-1 shadow-2xl"
      >
        {props.options.map((opt, index) => {
          const isSelected = opt.value === props.value;
          const isHi = index === highlight;
          return (
            <li key={opt.value} role="presentation">
              <button
                type="button"
                role="option"
                aria-selected={isSelected}
                disabled={opt.disabled}
                onMouseEnter={() => setHighlight(index)}
                onClick={() => commit(opt)}
                className={[
                  "flex w-full items-center px-3 py-2 text-left text-[14px] transition-colors",
                  opt.disabled ? "cursor-not-allowed opacity-40" : "cursor-pointer",
                  isHi || isSelected ? "bg-[#1e2430] text-slate-100" : "text-slate-200 hover:bg-[#1e2430]",
                  isSelected ? "font-semibold" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <span className="whitespace-nowrap">{opt.label}</span>
                {isSelected ? (
                  <span className="material-symbols-outlined ml-2 shrink-0 text-[var(--brand-accent)]" style={{ fontSize: 16 }}>
                    check
                  </span>
                ) : null}
              </button>
            </li>
          );
        })}
      </ul>
    ) : null;

  const lockedWidth = !props.fullWidth && panelWidth != null ? panelWidth : undefined;

  return (
    <div
      ref={rootRef}
      style={lockedWidth != null ? { width: lockedWidth } : undefined}
      className={["relative inline-block", props.fullWidth ? "w-full" : "", props.className ?? ""]
        .filter(Boolean)
        .join(" ")}
    >
      <div
        ref={measureRef}
        className="pointer-events-none fixed -left-[10000px] top-0 -z-50 opacity-0"
        aria-hidden
      >
        {props.options.map((opt) => (
          <React.Fragment key={opt.value}>
            <div
              data-brand-select-measure
              className={`inline-flex items-center whitespace-nowrap ${optionTextClass}`}
              style={{ paddingLeft: MENU_OPTION_PAD_PX / 2, paddingRight: MENU_OPTION_PAD_PX / 2 }}
            >
              <span>{opt.label}</span>
              <span style={{ width: MENU_CHECK_RESERVE_PX, display: "inline-block" }} />
            </div>
            <div
              data-brand-select-measure
              className={[
                "inline-flex items-center justify-between gap-2 whitespace-nowrap rounded-md border border-transparent",
                SIZE_CLASS[size],
                optionTextClass,
              ].join(" ")}
            >
              <span>{opt.label}</span>
              <span
                className="material-symbols-outlined shrink-0"
                style={{ fontSize: size === "sm" ? 16 : 18, width: size === "sm" ? 16 : 18 }}
                aria-hidden
              >
                expand_more
              </span>
            </div>
          </React.Fragment>
        ))}
      </div>
      <button
        ref={triggerRef}
        type="button"
        id={props.id}
        aria-label={props.ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        disabled={props.disabled}
        onClick={() => {
          if (!props.disabled) setOpen((v) => !v);
        }}
        onKeyDown={(e) => {
          if (props.disabled) return;
          if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            if (!open) setOpen(true);
            else if (e.key === "ArrowDown") moveHighlight(1);
            else if (e.key === "Enter" || e.key === " ") {
              const opt = props.options[highlight];
              if (opt) commit(opt);
            }
          } else if (e.key === "ArrowUp" && open) {
            e.preventDefault();
            moveHighlight(-1);
          }
        }}
        className={[
          "inline-flex items-center justify-between gap-2 rounded-md border border-[#424753]/60 bg-[#141a24] text-slate-200",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]",
          SIZE_CLASS[size],
          props.fullWidth || lockedWidth != null ? "w-full" : "",
          props.disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
          props.triggerClassName ?? "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <span className="min-w-0 truncate text-left">{label || "—"}</span>
        <span
          className="material-symbols-outlined shrink-0 text-slate-400"
          style={{ fontSize: size === "sm" ? 16 : 18 }}
          aria-hidden
        >
          {open ? "expand_less" : "expand_more"}
        </span>
      </button>

      {menu && typeof document !== "undefined" ? createPortal(menu, document.body) : null}
    </div>
  );
}