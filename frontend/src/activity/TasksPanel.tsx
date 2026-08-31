import { Fragment, useEffect, useState } from "react";
import type { Brand, ThemeMode } from "../brandTypes";
import { getBrandSemanticTokens } from "../brandSemanticTheme";
import type { ActiveSearchItem, ActiveSearchesResponse } from "../api/dashboard";
import type { ActivityRow, ScheduledTaskRow, TaskRunRow } from "../types/api";
import { formatTaskDuration, formatTaskTrigger, timeAgo, timeUntil } from "./formatActivityTime";

type StripAccent = { hex: string; text: string; icon: string; hoverHex: string };

function taskRunProgressSections(progress: ActivityRow["progress"] | undefined): Array<any> {
  if (!progress) return [];
  const inner = (progress as any).progress;
  if (inner && Array.isArray(inner.sections)) return inner.sections;
  if (Array.isArray((progress as any).sections)) return (progress as any).sections;
  return [];
}

function statusClass(status: string | null | undefined, semantic: { success: string; danger: string; accentIce: string; fgMuted: string }) {
  const t = String(status || "").toUpperCase();
  if (t === "DONE") return semantic.success;
  if (t === "FAILED") return semantic.danger;
  if (t === "SKIPPED") return "#d97706";
  if (t === "WORKING") return semantic.accentIce;
  return semantic.fgMuted;
}

function ActiveSearchesSection(props: {
  snapshot: ActiveSearchesResponse;
  semantic: ReturnType<typeof getBrandSemanticTokens>;
  isLight: boolean;
  panelBorder: string;
  panelBg: string;
}) {
  const { snapshot, semantic, isLight, panelBorder, panelBg } = props;
  const items: ActiveSearchItem[] = Array.isArray(snapshot.items) ? snapshot.items : [];
  const monitoring = Boolean(snapshot.active);

  return (
    <div className="rounded-xl border overflow-hidden mb-6" style={{ borderColor: panelBorder, backgroundColor: panelBg }}>
      <div className="px-4 py-3 border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.3)" }}>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-[20px] font-bold font-headline" style={{ color: isLight ? semantic.fg : "#fff" }}>
            Active searches
          </h2>
        </div>
        <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
          Titles Placeholdarr is watching after a playback search, not the full Radarr/Sonarr queue.
        </p>
      </div>
      {!monitoring ? (
        <div className="px-4 py-6 text-[14px]" style={{ color: semantic.fgSubtle }}>
          No titles being monitored
        </div>
      ) : items.length === 0 ? (
        <div className="px-4 py-4 text-[14px]" style={{ color: semantic.fgMuted }}>
          {snapshot.details}
        </div>
      ) : (
        <ul className="divide-y" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.15)" }}>
          {items.map((it, idx) => {
            const title = String(it.title || "—");
            const subtitle = String(it.subtitle || "").trim();
            const line = String(it.line || "—");
            const inst = String(it.instance || "").trim();
            return (
              <li key={`${title}-${subtitle}-${idx}`} className="px-4 py-3 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-medium truncate" style={{ color: semantic.fg }}>
                    {title}
                    {subtitle ? <span style={{ color: semantic.fgMuted }}> · {subtitle}</span> : null}
                  </div>
                  <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
                    {line}
                    {inst ? ` · ${inst}` : ""}
                  </p>
                </div>
                {it.arr_percent != null && Number.isFinite(Number(it.arr_percent)) ? (
                  <div
                    className="shrink-0 text-[12px] font-headline uppercase tracking-wider tabular-nums"
                    style={{ color: semantic.accentIce }}
                  >
                    {Math.round(Number(it.arr_percent))}%
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {monitoring && snapshot.started_at ? (
        <div
          className="px-4 py-2 text-[12px] border-t"
          style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.2)", color: semantic.fgSubtle }}
        >
          Monitoring since {timeAgo(snapshot.started_at)}
        </div>
      ) : null}
    </div>
  );
}

export function TasksPanel(props: {
  scheduled: ScheduledTaskRow[];
  history: TaskRunRow[];
  brand: Brand;
  themeMode: ThemeMode;
  accent: StripAccent;
  activeSearches: ActiveSearchesResponse;
  onRequestRun: (kind: "full" | "lite") => void;
  onRunCollections: () => Promise<void> | void;
  onRequestRefresh: (kind: "metadata" | "art" | "both") => Promise<void> | void;
}) {
  const semantic = getBrandSemanticTokens(props.brand, props.themeMode, props.accent);
  const isLight = props.themeMode === "light";
  const [historyExpanded, setHistoryExpanded] = useState<Record<string, boolean>>({});
  const panelBorder = isLight ? semantic.border : "rgba(66,71,83,0.4)";
  const panelBg = isLight ? semantic.surfacePanel : "#171c22";

  useEffect(() => {
    setHistoryExpanded((prev) => {
      const next = { ...prev };
      for (const run of props.history) {
        const key = `task-run-${run.id}`;
        const hasProgress = taskRunProgressSections(run.progress).length > 0;
        if (!hasProgress) continue;
        if (String(run.status).toUpperCase() === "WORKING" && next[key] === undefined) {
          next[key] = true;
        }
      }
      return next;
    });
  }, [props.history]);

  return (
    <div>
      <ActiveSearchesSection
        snapshot={props.activeSearches}
        semantic={semantic}
        isLight={isLight}
        panelBorder={panelBorder}
        panelBg={panelBg}
      />

      <div className="rounded-xl border overflow-hidden mb-6" style={{ borderColor: panelBorder, backgroundColor: panelBg }}>
        <div className="px-4 py-3 border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.3)" }}>
          <h2 className="text-[20px] font-bold font-headline" style={{ color: isLight ? semantic.fg : "#fff" }}>
            Scheduled
          </h2>
          <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
            Recurring maintenance. Use the buttons below to run actions now.
          </p>
        </div>
        <div className="px-4 py-3 border-b flex flex-wrap gap-2" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.2)" }}>
          <button
            type="button"
            onClick={() => props.onRequestRefresh("metadata")}
            className="px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider"
            style={{ borderColor: semantic.border, color: semantic.fg }}
          >
            Refresh metadata
          </button>
          <button
            type="button"
            onClick={() => props.onRequestRefresh("art")}
            className="px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider"
            style={{ borderColor: semantic.border, color: semantic.fg }}
          >
            Refresh art
          </button>
          <button
            type="button"
            onClick={() => props.onRequestRefresh("both")}
            className="px-3 py-1.5 rounded-lg text-[12px] uppercase tracking-wider"
            style={{ backgroundColor: props.accent.hex, color: semantic.fgOnAccent }}
          >
            Refresh all placeholders
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-[640px] w-full">
            <thead>
              <tr className="border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.2)" }}>
                {["Name", "Interval", "Last execution", "Last duration", "Next execution", ""].map((h) => (
                  <th
                    key={h}
                    className="px-3 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal"
                    style={{ color: semantic.fgSubtle }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {props.scheduled.map((task) => (
                <tr key={task.task_key} className="border-b last:border-0" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.15)" }}>
                  <td className="px-3 py-3 text-[15px] font-medium" style={{ color: semantic.fg }}>
                    {task.label}
                    {task.task_key === "lite_sync" ? (
                      <p className="text-[12px] font-normal mt-0.5 max-w-md" style={{ color: semantic.fgSubtle }}>
                        Catalog diff plus calendar date refresh and Coming Soon status updates.
                      </p>
                    ) : null}
                  </td>
                  <td className="px-3 py-3 text-[14px]" style={{ color: semantic.fgMuted }}>
                    {task.interval_label}
                  </td>
                  <td className="px-3 py-3 text-[14px] whitespace-nowrap" style={{ color: semantic.fgMuted }}>
                    {task.last_run ? timeAgo(task.last_run) : "--"}
                    {task.last_status ? (
                      <span className="ml-2 text-[11px] uppercase" style={{ color: statusClass(task.last_status, semantic) }}>
                        {task.last_status}
                      </span>
                    ) : null}
                  </td>
                  <td className="px-3 py-3 text-[14px] font-mono" style={{ color: semantic.fgMuted }}>
                    {formatTaskDuration(task.last_duration_seconds)}
                  </td>
                  <td className="px-3 py-3 text-[14px] whitespace-nowrap" style={{ color: semantic.fgMuted }}>
                    {task.running ? (
                      <span style={{ color: semantic.accentIce }}>Running now</span>
                    ) : task.next_run ? (
                      timeUntil(task.next_run)
                    ) : task.enabled ? (
                      "--"
                    ) : (
                      "Disabled"
                    )}
                  </td>
                  <td className="px-3 py-3 text-right">
                    <button
                      type="button"
                      disabled={!task.enabled || task.running}
                      onClick={() => {
                        if (task.task_key === "collections_sync") {
                          void props.onRunCollections();
                          return;
                        }
                        props.onRequestRun(task.task_key === "full_sync" ? "full" : "lite");
                      }}
                      className="inline-flex items-center justify-center w-9 h-9 rounded-lg border disabled:opacity-40"
                      style={{ borderColor: semantic.border, color: semantic.fgMuted }}
                      title="Run now"
                    >
                      <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                        refresh
                      </span>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="rounded-xl border overflow-hidden mb-6" style={{ borderColor: panelBorder, backgroundColor: panelBg }}>
        <div className="px-4 py-3 border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.3)" }}>
          <h2 className="text-[20px] font-bold font-headline" style={{ color: isLight ? semantic.fg : "#fff" }}>
            Maintenance runs
          </h2>
          <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
            Scheduled, manual, and startup. Expand a row for phase detail.
          </p>
        </div>
        {!props.history.length ? (
          <div className="p-10 text-center" style={{ color: semantic.fgSubtle }}>
            No task history yet.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-[640px] w-full">
              <thead>
                <tr className="border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.2)" }}>
                  {["Name", "Trigger", "Started", "Ended", "Duration", "Status"].map((h) => (
                    <th
                      key={h}
                      className="px-3 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal"
                      style={{ color: semantic.fgSubtle }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {props.history.map((run) => {
                  const rowKey = `task-run-${run.id}`;
                  const sections = taskRunProgressSections(run.progress);
                  const hasProgress = sections.length > 0;
                  const isExpanded = !!historyExpanded[rowKey];
                  return (
                    <Fragment key={run.id}>
                      <tr className="border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.15)" }}>
                        <td className="px-3 py-2 text-[14px]" style={{ color: semantic.fg }}>
                          <div className="flex items-start gap-2">
                            {hasProgress ? (
                              <button
                                type="button"
                                onClick={() => setHistoryExpanded((prev) => ({ ...prev, [rowKey]: !prev[rowKey] }))}
                                aria-label={isExpanded ? "Collapse details" : "Expand details"}
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: 16, color: semantic.fgMuted }}>
                                  {isExpanded ? "expand_less" : "expand_more"}
                                </span>
                              </button>
                            ) : (
                              <span className="w-4" />
                            )}
                            <span>{run.task_label}</span>
                          </div>
                        </td>
                        <td className="px-3 py-2 text-[14px]" style={{ color: semantic.fgMuted }}>
                          {formatTaskTrigger(run.trigger)}
                        </td>
                        <td className="px-3 py-2 text-[14px] whitespace-nowrap" style={{ color: semantic.fgMuted }}>
                          {timeAgo(run.started_at)}
                        </td>
                        <td className="px-3 py-2 text-[14px] whitespace-nowrap" style={{ color: semantic.fgMuted }}>
                          {run.ended_at ? timeAgo(run.ended_at) : "--"}
                        </td>
                        <td className="px-3 py-2 text-[14px] font-mono" style={{ color: semantic.fgMuted }}>
                          {formatTaskDuration(run.duration_seconds)}
                        </td>
                        <td className="px-3 py-2">
                          <span className="text-[12px] font-headline uppercase tracking-wider" style={{ color: statusClass(run.status, semantic) }}>
                            {run.status}
                          </span>
                          {run.error_message ? (
                            <p className="text-[11px] mt-0.5" style={{ color: semantic.danger }}>
                              {run.error_message}
                            </p>
                          ) : null}
                        </td>
                      </tr>
                      {hasProgress && isExpanded ? (
                        <tr>
                          <td colSpan={6} className="px-4 py-3" style={{ backgroundColor: isLight ? semantic.surfaceMuted : "#1a2028" }}>
                            <div className="space-y-2">
                              {sections.map((section: any, sidx: number) => (
                                <div
                                  key={`sec-${sidx}`}
                                  className="rounded border p-3"
                                  style={{ borderColor: semantic.border, backgroundColor: isLight ? semantic.surfacePanel : "#1b2431" }}
                                >
                                  <div className="flex justify-between gap-2 mb-1">
                                    <span className="text-[12px] font-headline uppercase tracking-wider" style={{ color: semantic.fg }}>
                                      {String(section?.name || "Step")}
                                    </span>
                                    <span className="text-[11px] uppercase" style={{ color: semantic.fgMuted }}>
                                      {String(section?.status || "pending")}
                                    </span>
                                  </div>
                                  {Array.isArray(section?.metrics)
                                    ? section.metrics.map((metric: any, midx: number) => (
                                        <div key={midx} className="flex justify-between text-[13px]" style={{ color: semantic.fgMuted }}>
                                          <span>{String(metric?.label || "Metric")}</span>
                                          <span style={{ color: semantic.fg }}>{String(metric?.value ?? "--")}</span>
                                        </div>
                                      ))
                                    : null}
                                </div>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
