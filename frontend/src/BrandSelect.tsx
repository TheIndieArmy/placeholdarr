import React, { useEffect, useId, useRef, useState } from "react";

export type BrandSelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

const SIZE_CLASS: Record<"sm" | "md", string> = {
  sm: "pl-2 pr-7 py-1 text-[12px] min-h-[1.75rem]",
  md: "pl-2.5 pr-8 py-1.5 text-[14px] min-h-[2.25rem]",
};

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
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);

  const selected = props.options.find((o) => o.value === props.value);
  const label = selected?.label ?? "";

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
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
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const idx = props.options.findIndex((o) => o.value === props.value);
    setHighlight(idx >= 0 ? idx : 0);
  }, [open, props.options, props.value]);

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

  return (
    <div
      ref={rootRef}
      className={["relative inline-block", props.fullWidth ? "w-full" : "", props.className ?? ""]
        .filter(Boolean)
        .join(" ")}
    >
      <button
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
          props.fullWidth ? "w-full" : "",
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

      {open ? (
        <ul
          id={listId}
          role="listbox"
          aria-label={props.ariaLabel}
          className="absolute z-50 mt-1 max-h-60 min-w-full overflow-y-auto rounded-xl border border-[#424753]/60 bg-[#11161d] py-1 shadow-2xl"
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
                  <span className="min-w-0 flex-1 truncate">{opt.label}</span>
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
      ) : null}
    </div>
  );
}
