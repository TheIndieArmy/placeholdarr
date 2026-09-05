import { useCallback, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  getMovieDetail,
  getSeriesDetail,
  refreshMoviePlaceholder,
  refreshSeriesPlaceholder,
} from "../../api/dashboard";
import type { Brand, ThemeMode } from "../../brandTypes";
import type { MovieDetailResponse, SeriesDetailResponse } from "../../types/api";
import { LibraryReconcileControl } from "../LibraryReconcileContext";
import { MovieDetailView } from "./MovieDetailView";
import { SeriesDetailView } from "./SeriesDetailView";

function getBrandAccentFromProps(accent: { hex: string; icon: string; label: string }) {
  return accent;
}

export function DetailRoutePage(props: {
  brand: Brand;
  themeMode: ThemeMode;
  accent: { hex: string; icon: string; label: string };
  scrollContainerRef: React.RefObject<HTMLElement | null>;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const accent = getBrandAccentFromProps(props.accent);
  const isLight = props.themeMode === "light";
  const [payload, setPayload] = useState<MovieDetailResponse | SeriesDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const pathParts = location.pathname.split("/");
  const entityType = pathParts[2] || "";
  const itemId = pathParts[3] || "";

  useEffect(() => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const container = props.scrollContainerRef.current;
        if (container) container.scrollTop = 0;
        window.scrollTo(0, 0);
        document.documentElement.scrollTop = 0;
        document.body.scrollTop = 0;
      });
    });
  }, [entityType, itemId, props.scrollContainerRef]);

  useEffect(() => {
    if (loading) return;
    const container = props.scrollContainerRef.current;
    if (container) container.scrollTop = 0;
    window.scrollTo(0, 0);
  }, [loading, props.scrollContainerRef]);

  const reloadDetail = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!entityType || !itemId) return;
      const numericId = Number(itemId);
      if (!Number.isFinite(numericId) || numericId <= 0) {
        setLoading(false);
        setPayload(null);
        setError("Invalid library item");
        return;
      }
      if (!opts?.silent) {
        setLoading(true);
      }
      setError(null);
      try {
        if (entityType === "movie") {
          const result = await getMovieDetail(numericId);
          if (result.ok) {
            setPayload(result);
          } else {
            const msg = (result as { message?: unknown }).message;
            setError(typeof msg === "string" && msg.trim() ? msg : "Movie not found");
          }
        } else if (entityType === "series") {
          const result = await getSeriesDetail(numericId);
          if (result.ok && result.type === "series") {
            setPayload(result);
          } else if (!result.ok) {
            const msg = (result as { message?: unknown }).message;
            setError(typeof msg === "string" && msg.trim() ? msg : "Series not found");
          } else {
            setError("Series not found");
          }
        } else {
          setError("Unsupported detail type");
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load detail");
      } finally {
        if (!opts?.silent) {
          setLoading(false);
        }
      }
    },
    [entityType, itemId],
  );

  useEffect(() => {
    let stopped = false;
    void (async () => {
      if (stopped) return;
      await reloadDetail();
    })();
    return () => {
      stopped = true;
    };
  }, [reloadDetail]);

  return (
    <div className={`min-h-screen ${isLight ? "bg-[#eef3f8]" : "bg-[#0f1419]"}`}>
      <div className={`px-6 py-4 border-b flex items-center gap-3 ${isLight ? "border-[#d7e2f0]" : "border-[#424753]/30"}`}>
        <button
          type="button"
          onClick={() => {
            sessionStorage.setItem("libraryScrollRestorePending", "1");
            navigate(-1);
          }}
          className={`flex items-center gap-1.5 text-[14px] font-headline uppercase tracking-wider transition-colors ${isLight ? "text-slate-500 hover:text-slate-900" : "text-slate-400 hover:text-white"}`}
        >
          <span className="material-symbols-outlined" style={{ fontSize: 16 }}>arrow_back</span>
          Library
        </button>
        <span className={isLight ? "text-slate-400" : "text-slate-600"}>/</span>
        <span className={`text-[14px] font-headline uppercase tracking-wider ${isLight ? "text-slate-700" : "text-slate-300"}`}>
          {loading ? "Loading..." : payload?.title || "Detail"}
        </span>
        <Link
          to={entityType === "series" ? "/library/tv" : "/library"}
          className={`ml-auto text-[12px] font-headline uppercase tracking-wider ${isLight ? "text-slate-500 hover:text-slate-800" : "text-slate-500 hover:text-slate-200"}`}
        >
          Browse
        </Link>
      </div>
      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="flex items-center gap-3 text-slate-400">
            <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
            <span className="text-[16px] font-headline uppercase tracking-widest">Loading detail...</span>
          </div>
        </div>
      ) : null}
      {error ? (
        <div
          className={`mx-6 mt-4 rounded-xl border p-4 text-[16px] ${
            isLight ? "border-red-200 bg-red-50 text-red-800" : "border-red-500/30 bg-red-600/15 text-red-300"
          }`}
        >
          {error}
        </div>
      ) : null}
      {!loading && !error && payload?.type === "movie" ? (
        <>
          <MovieDetailView
            payload={payload}
            brand={props.brand}
            themeMode={props.themeMode}
            accent={accent}
            onPolicyApplied={() => {
              void reloadDetail({ silent: true });
            }}
          />
          <div className="px-6 md:px-10 lg:px-12 pb-10 -mt-2">
            <LibraryReconcileControl
              label="Refresh placeholder"
              startReconcile={() => refreshMoviePlaceholder(payload.id)}
            />
          </div>
        </>
      ) : null}
      {!loading && !error && payload?.type === "series" ? (
        <>
          <SeriesDetailView
            payload={payload}
            brand={props.brand}
            themeMode={props.themeMode}
            accent={accent}
            onPolicyApplied={() => {
              void reloadDetail({ silent: true });
            }}
          />
          <div className="px-6 md:px-10 lg:px-12 pb-10 -mt-2">
            <LibraryReconcileControl
              label="Refresh series placeholders"
              startReconcile={() => refreshSeriesPlaceholder(payload.id)}
            />
          </div>
        </>
      ) : null}
    </div>
  );
}
