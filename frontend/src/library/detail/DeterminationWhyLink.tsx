import { useState } from "react";
import type { ThemeMode } from "../../brandTypes";
import type { DeterminationExplainResponse } from "../../types/api";
import { getEpisodeDeterminationExplain, getMovieDeterminationExplain } from "../../api/dashboard";
import { DeterminationExplainModal } from "./DeterminationExplainModal";

export function DeterminationWhyLink(props: {
  mediaType: "movie" | "episode";
  entityId: number;
  determination?: string | null;
  themeMode: ThemeMode;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DeterminationExplainResponse | null>(null);
  const isLight = props.themeMode === "light";

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
        className={`shrink-0 text-[11px] font-headline uppercase tracking-wider ${
          isLight ? "text-slate-500 hover:text-sky-700" : "text-slate-500 hover:text-sky-300"
        }`}
      >
        Why?
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
