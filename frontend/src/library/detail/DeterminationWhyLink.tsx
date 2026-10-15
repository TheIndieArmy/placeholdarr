import { useState, type ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";
import type { DeterminationExplainResponse } from "../../types/api";
import { getEpisodeDeterminationExplain, getMovieDeterminationExplain } from "../../api/dashboard";
import { DeterminationExplainModal } from "./DeterminationExplainModal";

export function DeterminationWhyLink(props: {
  mediaType: "movie" | "episode";
  entityId: number;
  determination?: string | null;
  themeMode: ThemeMode;
  /** Custom trigger content (e.g. status chip). Defaults to a Why? text link. */
  children?: ReactNode;
  className?: string;
  /** Native hover/focus tip. Defaults for chip triggers. */
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DeterminationExplainResponse | null>(null);
  const isLight = props.themeMode === "light";
  const hasCustomTrigger = props.children != null;
  const tip =
    props.title ?? (hasCustomTrigger ? "Why this status?" : undefined);
  const defaultClass = `shrink-0 text-[11px] font-headline uppercase tracking-wider ${
    isLight ? "text-slate-500 hover:text-sky-700" : "text-slate-500 hover:text-sky-300"
  }`;

  const openModal = () => {
    setOpen(true);
    setLoading(true);
    setError(null);
    setResult(null);
    const fetcher =
      props.mediaType === "movie"
        ? () => getMovieDeterminationExplain(props.entityId)
        : () => getEpisodeDeterminationExplain(props.entityId);
    void fetcher()
      .then((payload) => {
        setResult(payload);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Could not load explanation");
        setLoading(false);
      });
  };

  return (
    <>
      <button
        type="button"
        onClick={openModal}
        title={tip}
        aria-label={tip || "Why this determination?"}
        className={
          props.className ??
          defaultClass
        }
      >
        {props.children ?? "Why?"}
      </button>
      <DeterminationExplainModal
        open={open}
        onClose={() => setOpen(false)}
        result={result}
        loading={loading}
        error={error}
        determination={props.determination}
        themeMode={props.themeMode}
      />
    </>
  );
}
