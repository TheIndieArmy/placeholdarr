import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  formatHhmm,
  formatHhmmDisplay,
  parseClockFormat,
  parseHhmm,
  type ClockFormat,
} from "./clockFormat";

const HOURS_24 = Array.from({ length: 24 }, (_, i) => i);
const HOURS_12 = Array.from({ length: 12 }, (_, i) => (i === 0 ? 12 : i));
const MINUTES = Array.from({ length: 60 }, (_, i) => i);

const FIELD_FOCUS =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]";

const OPTION_CLASS = (active: boolean) =>
  [
    "flex w-full items-center justify-center px-2 py-1.5 text-[13px] tabular-nums transition-colors",
    active
      ? "bg-[#1e2430] font-semibold text-[var(--brand-accent)]"
      : "text-slate-300 hover:bg-[#1e2430]",
  ].join(" ");

function to12HourParts(h24: number): { hour12: number; period: "AM" | "PM" } {
  const period: "AM" | "PM" = h24 < 12 ? "AM" : "PM";
  let hour12 = h24 % 12;
  if (hour12 === 0) hour12 = 12;
  return { hour12, period };
}

function from12HourParts(hour12: number, period: "AM" | "PM"): number {
  const base = hour12 % 12; // 12 -> 0
  return period === "AM" ? base : base + 12;
}

/**
 * Column that scrolls without ever painting a native scrollbar.
 * Uses overflow:hidden + non-passive wheel/touch handlers (CSS scrollbar hiding is unreliable here).
 */
function TimeColumn(props: {
  listRef?: React.RefObject<HTMLDivElement | null>;
  widthClass: string;
  showDivider?: boolean;
  ariaLabel: string;
  children: React.ReactNode;
}) {
  const localRef = useRef<HTMLDivElement | null>(null);
  const touchY = useRef<number | null>(null);

  const setRefs = (node: HTMLDivElement | null) => {
    localRef.current = node;
    if (props.listRef) {
      (props.listRef as React.MutableRefObject<HTMLDivElement | null>).current = node;
    }
  };

  useEffect(() => {
    const el = localRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      el.scrollTop += e.deltaY;
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  return (
    <div
      ref={setRefs}
      role="listbox"
      aria-label={props.ariaLabel}
      className={[
        "max-h-48 overflow-hidden overscroll-contain py-1",
        props.widthClass,
        props.showDivider ? "border-r border-[#424753]/40" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onTouchStart={(e) => {
        touchY.current = e.touches[0]?.clientY ?? null;
      }}
      onTouchMove={(e) => {
        const y = e.touches[0]?.clientY;
        if (y == null || touchY.current == null) return;
        e.currentTarget.scrollTop += touchY.current - y;
        touchY.current = y;
      }}
      onTouchEnd={() => {
        touchY.current = null;
      }}
    >
      {props.children}
    </div>
  );
}

/** Branded HH:MM time picker (no native browser time input). */
export function BrandTimeSelect(props: {
  value: string;
  onChange: (time: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
  /** Prefer 12h or 24h labels. Defaults to 12h. */
  clockFormat?: ClockFormat | string | null;
}) {
  const clockFormat = parseClockFormat(props.clockFormat);
  const is12h = clockFormat === "12h";
  const { h, m } = useMemo(() => parseHhmm(props.value), [props.value]);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [draftH, setDraftH] = useState(h);
  const [draftM, setDraftM] = useState(m);
  const hourListRef = useRef<HTMLDivElement | null>(null);
  const minuteListRef = useRef<HTMLDivElement | null>(null);

  const { hour12: draftHour12, period: draftPeriod } = to12HourParts(draftH);

  useEffect(() => {
    if (!open) return;
    setDraftH(h);
    setDraftM(m);
  }, [open, h, m]);

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
    const scrollSelected = (el: HTMLDivElement | null, index: number) => {
      const child = el?.children[index] as HTMLElement | undefined;
      if (!el || !child) return;
      const top = child.offsetTop - el.clientHeight / 2 + child.offsetHeight / 2;
      el.scrollTop = Math.max(0, top);
    };
    requestAnimationFrame(() => {
      const hourIndex = is12h ? (draftHour12 === 12 ? 0 : draftHour12) : draftH;
      scrollSelected(hourListRef.current, hourIndex);
      scrollSelected(minuteListRef.current, draftM);
    });
  }, [open, draftH, draftM, draftHour12, is12h]);

  const apply = (nextH: number, nextM: number, close = false) => {
    setDraftH(nextH);
    setDraftM(nextM);
    props.onChange(formatHhmm(nextH, nextM));
    if (close) setOpen(false);
  };

  return (
    <div ref={rootRef} className={["relative inline-block", props.className ?? ""].filter(Boolean).join(" ")}>
      <button
        type="button"
        aria-label={props.ariaLabel}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={listId}
        disabled={props.disabled}
        onClick={() => {
          if (!props.disabled) setOpen((v) => !v);
        }}
        className={[
          "inline-flex items-center gap-2 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-2.5 py-1.5 text-[14px] text-slate-200",
          FIELD_FOCUS,
          props.disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <span className="material-symbols-outlined text-slate-400" style={{ fontSize: 18 }} aria-hidden>
          schedule
        </span>
        <span>{formatHhmmDisplay(formatHhmm(h, m), clockFormat)}</span>
        <span className="material-symbols-outlined text-slate-500" style={{ fontSize: 16 }} aria-hidden>
          {open ? "expand_less" : "expand_more"}
        </span>
      </button>

      {open ? (
        <div
          id={listId}
          role="dialog"
          aria-label={props.ariaLabel || "Choose time"}
          className="absolute z-50 mt-1 flex overflow-hidden rounded-xl border border-[#424753]/60 bg-[#11161d] shadow-2xl"
        >
          <TimeColumn
            listRef={hourListRef}
            widthClass="w-[4.25rem]"
            showDivider
            ariaLabel="Hour"
          >
            {(is12h ? HOURS_12 : HOURS_24).map((hour) => {
              const active = is12h ? hour === draftHour12 : hour === draftH;
              return (
                <button
                  key={hour}
                  type="button"
                  role="option"
                  aria-selected={active}
                  onClick={() => {
                    if (is12h) {
                      apply(from12HourParts(hour, draftPeriod), draftM);
                    } else {
                      apply(hour, draftM);
                    }
                  }}
                  className={OPTION_CLASS(active)}
                >
                  {is12h ? String(hour) : String(hour).padStart(2, "0")}
                </button>
              );
            })}
          </TimeColumn>
          <TimeColumn
            listRef={minuteListRef}
            widthClass="w-[4.25rem]"
            showDivider={is12h}
            ariaLabel="Minute"
          >
            {MINUTES.map((minute) => {
              const active = minute === draftM;
              return (
                <button
                  key={minute}
                  type="button"
                  role="option"
                  aria-selected={active}
                  onClick={() => apply(draftH, minute, !is12h)}
                  className={OPTION_CLASS(active)}
                >
                  {String(minute).padStart(2, "0")}
                </button>
              );
            })}
          </TimeColumn>
          {is12h ? (
            <TimeColumn widthClass="w-[3.75rem]" ariaLabel="AM or PM">
              {(["AM", "PM"] as const).map((period) => {
                const active = period === draftPeriod;
                return (
                  <button
                    key={period}
                    type="button"
                    role="option"
                    aria-selected={active}
                    onClick={() => apply(from12HourParts(draftHour12, period), draftM, true)}
                    className={OPTION_CLASS(active)}
                  >
                    {period}
                  </button>
                );
              })}
            </TimeColumn>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
