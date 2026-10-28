import React, { useState } from "react";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import { BrandTimeSelect } from "../BrandTimeSelect";
import { formatHhmmDisplay, parseClockFormat, type ClockFormat } from "../clockFormat";
import { ToggleSwitch } from "../ToggleSwitch";

export type ScheduleMode = "interval" | "daily" | "weekly" | "monthly" | "cron";

export type TaskScheduleValue = {
  enabled: boolean;
  mode: ScheduleMode;
  interval_hours: number | null;
  time: string | null;
  weekdays: number[] | null;
  /** Read-only compat for legacy singular weekday; not written. */
  weekday?: number | null;
  monthday: number | null;
  cron: string | null;
};

const WEEKDAYS = [
  { value: 0, label: "Mon" },
  { value: 1, label: "Tue" },
  { value: 2, label: "Wed" },
  { value: 3, label: "Thu" },
  { value: 4, label: "Fri" },
  { value: 5, label: "Sat" },
  { value: 6, label: "Sun" },
];

const MODE_OPTIONS: { value: ScheduleMode; label: string }[] = [
  { value: "interval", label: "Interval" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "cron", label: "Cron" },
];

const FIELD_FOCUS =
  "outline-none transition-colors focus:ring-2 focus:ring-offset-0 focus:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]";

const INPUT_CLASS =
  `bg-[#0f1419] border border-[#424753]/40 rounded-lg px-2.5 py-1.5 text-[14px] text-slate-200 w-20 ${FIELD_FOCUS}`;

const CRON_INPUT_CLASS =
  `bg-[#0f1419] border border-[#424753]/40 rounded-lg px-2.5 py-1.5 text-[14px] text-slate-200 w-full max-w-md font-mono ${FIELD_FOCUS}`;

const CRON_HELP_EXAMPLES: { expr: string; meaning: string }[] = [
  { expr: "0 3 * * *", meaning: "Every day at 03:00" },
  { expr: "0 */6 * * *", meaning: "Every 6 hours" },
  { expr: "0 3 * * 0", meaning: "Mondays at 03:00" },
];

function CronExpressionHelp() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        className="inline-flex items-center gap-1 text-[12px] font-headline uppercase tracking-wider text-slate-400 transition hover:text-slate-200"
        aria-expanded={open}
        aria-controls="cron-expression-help"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="material-symbols-outlined" style={{ fontSize: 16 }} aria-hidden>
          {open ? "expand_less" : "expand_more"}
        </span>
        Cron help
      </button>
      {open ? (
        <div
          id="cron-expression-help"
          className="mt-1.5 max-w-md space-y-2 rounded-lg border border-[#424753]/40 bg-[#0f1419]/80 px-3 py-2.5 text-[13px] leading-snug text-slate-400"
        >
          <p>
            Five fields, in order: minute, hour, day-of-month, month, day-of-week (APScheduler;
            Monday=0).
          </p>
          <ul className="list-disc space-y-1 pl-5">
            <li>
              <span className="font-mono text-slate-300">*</span> any value
            </li>
            <li>
              <span className="font-mono text-slate-300">,</span> list (e.g.{" "}
              <span className="font-mono text-slate-300">1,15</span>)
            </li>
            <li>
              <span className="font-mono text-slate-300">-</span> range (e.g.{" "}
              <span className="font-mono text-slate-300">1-5</span>)
            </li>
            <li>
              <span className="font-mono text-slate-300">/</span> step (e.g.{" "}
              <span className="font-mono text-slate-300">*/6</span>)
            </li>
          </ul>
          <div className="space-y-1 pt-0.5">
            <p className="text-[12px] font-headline uppercase tracking-wider text-slate-500">Examples</p>
            <ul className="space-y-1">
              {CRON_HELP_EXAMPLES.map((row) => (
                <li key={row.expr}>
                  <span className="font-mono text-slate-300">{row.expr}</span>
                  <span className="text-slate-500"> · </span>
                  {row.meaning}
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function disabledSchedule(): TaskScheduleValue {
  return {
    enabled: false,
    mode: "interval",
    interval_hours: null,
    time: null,
    weekdays: null,
    monthday: null,
    cron: null,
  };
}

function applyScheduleMode(
  base: TaskScheduleValue,
  mode: ScheduleMode,
): TaskScheduleValue {
  const enabled = base.enabled;
  const time = base.time || "03:00";
  if (!enabled) {
    return { ...disabledSchedule(), enabled: false, mode };
  }
  if (mode === "interval") {
    return {
      enabled: true,
      mode: "interval",
      interval_hours: Math.max(1, base.interval_hours || 12),
      time: null,
      weekdays: null,
      monthday: null,
      cron: null,
    };
  }
  if (mode === "daily") {
    return {
      enabled: true,
      mode: "daily",
      interval_hours: null,
      time,
      weekdays: null,
      monthday: null,
      cron: null,
    };
  }
  if (mode === "weekly") {
    const weekdays =
      base.weekdays && base.weekdays.length
        ? base.weekdays
        : base.weekday != null
          ? [base.weekday]
          : [6];
    return {
      enabled: true,
      mode: "weekly",
      interval_hours: null,
      time,
      weekdays,
      monthday: null,
      cron: null,
    };
  }
  if (mode === "monthly") {
    return {
      enabled: true,
      mode: "monthly",
      interval_hours: null,
      time,
      weekdays: null,
      monthday: base.monthday == null ? 1 : base.monthday,
      cron: null,
    };
  }
  return {
    enabled: true,
    mode: "cron",
    interval_hours: null,
    time: null,
    weekdays: null,
    monthday: null,
    cron: base.cron || "0 3 * * *",
  };
}

/** Migrate legacy hour ints and WIP every/unit into mode schedules. */
export function scheduleFromLegacyHours(hours: number): TaskScheduleValue {
  const n = Math.max(0, Math.floor(Number(hours) || 0));
  if (n <= 0) return disabledSchedule();
  if (n === 12) {
    return applyScheduleMode({ ...disabledSchedule(), enabled: true, interval_hours: 12 }, "interval");
  }
  if (n % 168 === 0) {
    return applyScheduleMode(
      { ...disabledSchedule(), enabled: true, time: "03:00", weekdays: [6] },
      "weekly",
    );
  }
  if (n % 24 === 0) {
    return applyScheduleMode({ ...disabledSchedule(), enabled: true, time: "03:00" }, "daily");
  }
  return applyScheduleMode(
    { ...disabledSchedule(), enabled: true, interval_hours: n },
    "interval",
  );
}

export function parseTaskSchedule(raw: unknown, fallbackHours = 24): TaskScheduleValue {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const o = raw as Record<string, unknown>;
    const enabled = o.enabled !== false && o.enabled !== 0 && o.enabled !== "0";
    if (!enabled) return disabledSchedule();

    // WIP every/unit
    if (o.unit != null && (o.mode == null || o.mode === "")) {
      const unit = String(o.unit);
      const every = Math.max(1, Number(o.every ?? o.interval_hours ?? 1) || 1);
      const time = o.time == null || o.time === "" ? "03:00" : String(o.time);
      if (unit === "hours") {
        return applyScheduleMode(
          { ...disabledSchedule(), enabled: true, interval_hours: every },
          "interval",
        );
      }
      if (unit === "days") {
        return every === 1
          ? applyScheduleMode({ ...disabledSchedule(), enabled: true, time }, "daily")
          : applyScheduleMode(
              { ...disabledSchedule(), enabled: true, interval_hours: every * 24 },
              "interval",
            );
      }
      if (unit === "weeks") {
        let weekdays: number[] = [6];
        if (Array.isArray(o.weekdays)) {
          weekdays = o.weekdays.map((d) => Number(d)).filter((d) => d >= 0 && d <= 6);
        } else if (o.weekday != null) {
          weekdays = [Number(o.weekday)];
        }
        return applyScheduleMode(
          { ...disabledSchedule(), enabled: true, time, weekdays },
          "weekly",
        );
      }
      if (unit === "months") {
        return applyScheduleMode(
          {
            ...disabledSchedule(),
            enabled: true,
            time,
            monthday: o.monthday == null ? 1 : Number(o.monthday),
          },
          "monthly",
        );
      }
    }

    const modeRaw = String(o.mode || "interval");
    const mode = (
      ["interval", "daily", "weekly", "monthly", "cron"].includes(modeRaw) ? modeRaw : "interval"
    ) as ScheduleMode;

    let weekdays: number[] | null = null;
    if (Array.isArray(o.weekdays)) {
      weekdays = o.weekdays.map((d) => Number(d)).filter((d) => d >= 0 && d <= 6);
    } else if (o.weekday != null) {
      weekdays = [Number(o.weekday)];
    }

    const draft: TaskScheduleValue = {
      enabled: true,
      mode,
      interval_hours:
        o.interval_hours == null || o.interval_hours === ""
          ? mode === "interval"
            ? fallbackHours || 12
            : null
          : Number(o.interval_hours),
      time: o.time == null || o.time === "" ? (mode === "interval" || mode === "cron" ? null : "03:00") : String(o.time),
      weekdays,
      monthday: o.monthday == null ? (mode === "monthly" ? 1 : null) : Number(o.monthday),
      cron: o.cron == null || o.cron === "" ? (mode === "cron" ? "0 3 * * *" : null) : String(o.cron),
    };
    return applyScheduleMode(draft, mode);
  }
  return scheduleFromLegacyHours(fallbackHours);
}

/** One-line TZ note for a settings section (not per schedule field). */
export function ScheduleTimezoneNote(props: { name?: string | null; className?: string }) {
  const name = String(props.name || "").trim() || "UTC";
  return (
    <p className={props.className || "text-[12px] text-slate-400"}>
      Timezone: <span className="text-slate-200">{name}</span>
    </p>
  );
}

type Props = {
  value: unknown;
  onChange: (next: TaskScheduleValue) => void;
  /** When true, Off means "follow global" (null) instead of disabled schedule. */
  allowFollowGlobal?: boolean;
  followGlobal?: boolean;
  onFollowGlobalChange?: (follow: boolean) => void;
  disabled?: boolean;
  className?: string;
  /** Brand accent fill for toggles. Defaults to CSS `--brand-accent`. */
  accentHex?: string;
  fallbackHours?: number;
  /** Dashboard clock format (12h / 24h). */
  clockFormat?: ClockFormat | string | null;
};

export function TaskScheduleEditor(props: Props) {
  const follow = Boolean(props.allowFollowGlobal && props.followGlobal);
  const sched = parseTaskSchedule(props.value, props.fallbackHours ?? 24);
  const disabled = Boolean(props.disabled) || follow;
  const accentHex = props.accentHex || "var(--brand-accent)";
  const fieldsDisabled = disabled || !sched.enabled;
  const mode = sched.mode;
  const clockFormat = parseClockFormat(props.clockFormat);

  const commit = (next: TaskScheduleValue) => {
    props.onChange(next);
  };

  const setMode = (nextMode: ScheduleMode) => {
    commit(applyScheduleMode({ ...sched, enabled: true }, nextMode));
  };

  const toggleWeekday = (day: number, on: boolean) => {
    const cur = new Set(sched.weekdays || []);
    if (on) cur.add(day);
    else cur.delete(day);
    const next = Array.from(cur).sort((a, b) => a - b);
    commit(
      applyScheduleMode(
        { ...sched, weekdays: next.length ? next : [day] },
        "weekly",
      ),
    );
  };

  const timeLabel = formatHhmmDisplay(sched.time || "03:00", clockFormat);

  return (
    <div className={props.className || "space-y-3"}>
      {props.allowFollowGlobal ? (
        <div className="space-y-1.5">
          <label className={`flex items-center gap-3 text-sm text-slate-200 select-none w-fit ${props.disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}>
            <ToggleSwitch
              checked={follow}
              disabled={props.disabled}
              accentHex={accentHex}
              ariaLabel="Follow Collections sync schedule"
              onChange={(checked) => props.onFollowGlobalChange?.(checked)}
            />
            Follow Collections sync schedule
          </label>
          <p className="text-[12px] text-slate-500 leading-relaxed">
            {follow
              ? "Uses Settings → Library sync → Collections sync schedule. If that schedule is disabled, this recipe only runs manually."
              : "Uses this recipe’s own schedule, even when the Collections sync schedule in Settings is disabled."}
          </p>
        </div>
      ) : null}

      {!follow ? (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <label className={`flex items-center gap-3 text-sm text-slate-200 select-none w-fit ${disabled ? "cursor-not-allowed" : "cursor-pointer"}`}>
              <ToggleSwitch
                checked={sched.enabled}
                disabled={disabled}
                accentHex={accentHex}
                ariaLabel="Schedule enabled"
                onChange={(checked) =>
                  commit(
                    checked
                      ? applyScheduleMode({ ...sched, enabled: true }, mode)
                      : disabledSchedule(),
                  )
                }
              />
              <span className={`inline-block min-w-[4.75rem] ${disabled ? "text-slate-500" : "text-slate-300"}`}>
                {sched.enabled ? "Enabled" : "Disabled"}
              </span>
            </label>
          </div>

          {sched.enabled ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2 text-sm text-slate-300">
                <span>Schedule</span>
                <BrandSelect
                  size="sm"
                  value={mode}
                  options={MODE_OPTIONS}
                  disabled={fieldsDisabled}
                  ariaLabel="Schedule mode"
                  triggerClassName="min-w-[8.5rem]"
                  onChange={(m) => setMode(m as ScheduleMode)}
                />
              </div>

              {mode === "interval" ? (
                <div className="flex flex-wrap items-center gap-2 text-sm text-slate-300">
                  <span>Every</span>
                  <input
                    type="number"
                    min={1}
                    className={`${INPUT_CLASS} ${fieldsDisabled ? "cursor-not-allowed opacity-50" : ""}`}
                    disabled={fieldsDisabled}
                    value={sched.interval_hours ?? 12}
                    onChange={(e) =>
                      commit(
                        applyScheduleMode(
                          {
                            ...sched,
                            interval_hours: Math.max(1, Number(e.target.value) || 1),
                          },
                          "interval",
                        ),
                      )
                    }
                    aria-label="Interval hours"
                  />
                  <span>hours</span>
                </div>
              ) : null}

              {mode === "daily" || mode === "weekly" || mode === "monthly" ? (
                <div className="flex flex-wrap items-center gap-2 text-sm text-slate-300">
                  <span>At</span>
                  <BrandTimeSelect
                    value={sched.time || "03:00"}
                    disabled={fieldsDisabled}
                    ariaLabel="Schedule time"
                    clockFormat={clockFormat}
                    onChange={(time) => commit(applyScheduleMode({ ...sched, time }, mode))}
                  />
                </div>
              ) : null}

              {mode === "weekly" ? (
                <div className="space-y-1.5">
                  <div className="text-[12px] font-headline uppercase tracking-wider text-slate-500">
                    On days
                  </div>
                  <div className="flex flex-wrap gap-3">
                    {WEEKDAYS.map((d) => {
                      const checked = (sched.weekdays || []).includes(d.value);
                      return (
                        <label
                          key={d.value}
                          className={`flex items-center gap-1.5 text-[13px] select-none ${
                            fieldsDisabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
                          } text-slate-300`}
                        >
                          <BrandCheckbox
                            size="sm"
                            checked={checked}
                            disabled={fieldsDisabled}
                            accentHex={accentHex}
                            ariaLabel={d.label}
                            onChange={(on) => toggleWeekday(d.value, on)}
                          />
                          {d.label}
                        </label>
                      );
                    })}
                  </div>
                </div>
              ) : null}

              {mode === "monthly" ? (
                <div className="flex flex-wrap items-center gap-2 text-sm text-slate-300">
                  <span>On day</span>
                  <input
                    type="number"
                    min={1}
                    max={31}
                    className={`${INPUT_CLASS} ${fieldsDisabled ? "cursor-not-allowed opacity-50" : ""}`}
                    disabled={fieldsDisabled}
                    value={sched.monthday ?? 1}
                    onChange={(e) =>
                      commit(
                        applyScheduleMode(
                          {
                            ...sched,
                            monthday: Math.max(1, Math.min(31, Number(e.target.value) || 1)),
                          },
                          "monthly",
                        ),
                      )
                    }
                    aria-label="Day of month"
                  />
                  <span className="text-[12px] text-slate-500">
                    Shorter months run on the last day.
                  </span>
                </div>
              ) : null}

              {mode === "cron" ? (
                <div className="space-y-1.5">
                  <label className="block text-[12px] font-headline uppercase tracking-wider text-slate-500">
                    Cron expression (5 fields)
                  </label>
                  <input
                    type="text"
                    className={`${CRON_INPUT_CLASS} ${fieldsDisabled ? "cursor-not-allowed opacity-50" : ""}`}
                    disabled={fieldsDisabled}
                    value={sched.cron || ""}
                    placeholder="0 3 * * 0"
                    onChange={(e) =>
                      commit(
                        applyScheduleMode(
                          { ...sched, cron: e.target.value },
                          "cron",
                        ),
                      )
                    }
                    aria-label="Cron expression"
                  />
                  <CronExpressionHelp />
                </div>
              ) : null}

              <p className="text-[12px] text-slate-500">
                {mode === "interval"
                  ? `Runs every ${sched.interval_hours ?? 12} hour${(sched.interval_hours ?? 12) === 1 ? "" : "s"} from the last run.`
                  : mode === "daily"
                    ? `Runs every day at ${timeLabel}.`
                    : mode === "weekly"
                      ? `Runs on selected days at ${timeLabel}.`
                      : mode === "monthly"
                        ? `Runs on day ${sched.monthday ?? 1} each month at ${timeLabel}.`
                        : "Runs on the cron expression."}
              </p>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
