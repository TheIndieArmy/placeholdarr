import { useEffect, useState } from "react";
import { previewCollectionDefinition, previewDiscoverShelf, type DiscoverShelfLayout } from "../../api/collections";
import type { CollectionAutoAddConfig, CollectionDefinition, CollectionPreviewResponse } from "../../types/api";

const DEBOUNCE_MS = 350;

export type PreviewState = {
  preview: CollectionPreviewResponse | null;
  loading: boolean;
  error: string | null;
};

function useDebouncedPreview(
  requestKey: string,
  enabled: boolean,
  run: () => Promise<CollectionPreviewResponse>,
): PreviewState {
  const [preview, setPreview] = useState<CollectionPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setPreview(null);
      setError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      run()
        .then((result) => {
          if (!cancelled) setPreview(result);
        })
        .catch((err) => {
          if (!cancelled) {
            setPreview(null);
            setError(err instanceof Error ? err.message : "Preview failed");
          }
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey, enabled]);

  return { preview, loading, error };
}

/** Debounced live preview for one recipe definition (a recipe, a set child, or a Discover set child). */
export function useRecipePreview(
  definition: CollectionDefinition | null | undefined,
  sectionIds: number[],
  sectionType: "movie" | "show",
): PreviewState {
  const sectionId = sectionIds[0];
  const key = JSON.stringify({ definition: definition ?? null, sectionIds, sectionType });
  return useDebouncedPreview(key, Boolean(sectionId && definition), () =>
    previewCollectionDefinition({
      plex_section_id: sectionId,
      plex_section_ids: sectionIds,
      plex_section_type: sectionType,
      definition: definition as CollectionDefinition,
    }),
  );
}

/** Debounced preview of one Discover row exactly as Apply would create it. */
export function useDiscoverShelfPreview(args: {
  shelfId: string | null;
  isSet: boolean;
  sectionType: "movie" | "show";
  sectionId: number | "";
  layout: DiscoverShelfLayout | undefined;
  setValues: string[] | undefined;
  autoAdd: CollectionAutoAddConfig | null;
  autoAddReady: boolean;
}): PreviewState {
  const autoAdd = args.autoAddReady && args.autoAdd ? { [args.sectionType]: args.autoAdd } : undefined;
  const setValues = args.isSet && args.shelfId && args.setValues?.length ? { [args.shelfId]: args.setValues } : undefined;
  const key = JSON.stringify({
    shelfId: args.shelfId,
    sectionId: args.sectionId,
    layout: args.layout ?? null,
    setValues: setValues ?? null,
    autoAdd: autoAdd ?? null,
  });
  return useDebouncedPreview(key, Boolean(args.shelfId) && args.sectionId !== "", () =>
    previewDiscoverShelf({
      shelf_id: args.shelfId as string,
      section_id: Number(args.sectionId),
      set_values: setValues,
      shelf_layout: args.layout,
      auto_add: autoAdd,
    }),
  );
}
