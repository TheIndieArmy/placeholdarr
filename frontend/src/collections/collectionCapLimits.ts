/** Upper bound on titles per collection from library + not-in-library caps (null = no finite cap). */
export function maxCappedTitlesPerCollection(params: {
  includeLibrary: boolean;
  libraryLimit: number | null | undefined;
  autoAddEnabled: boolean;
  missingLimit: number | null | undefined;
}): number | null {
  const includeLibrary = params.includeLibrary;
  const autoAddEnabled = params.autoAddEnabled;
  if (!includeLibrary && !autoAddEnabled) {
    return 0;
  }
  let total = 0;
  let libraryUnbounded = false;
  if (includeLibrary) {
    const cap = params.libraryLimit;
    if (cap == null || cap < 1) {
      libraryUnbounded = true;
    } else {
      total += cap;
    }
  }
  if (autoAddEnabled) {
    const missing = params.missingLimit ?? 10;
    total += Math.max(0, missing);
  }
  if (libraryUnbounded) {
    return null;
  }
  return total;
}

export function minItemsVersusCapsConflict(params: {
  minItems: number | null | undefined;
  includeLibrary: boolean;
  libraryLimit: number | null | undefined;
  autoAddEnabled: boolean;
  missingLimit: number | null | undefined;
}): { short: string; title: string } | null {
  const minItems = params.minItems ?? 0;
  if (minItems < 1) {
    return null;
  }
  const max = maxCappedTitlesPerCollection(params);
  if (max == null || minItems <= max) {
    return null;
  }
  const parts: string[] = [];
  if (params.includeLibrary && params.libraryLimit != null && params.libraryLimit >= 1) {
    parts.push(`${params.libraryLimit} in your library`);
  }
  if (params.autoAddEnabled) {
    const missing = params.missingLimit ?? 10;
    parts.push(`${missing} not in your library`);
  }
  const capPhrase =
    parts.length > 0
      ? `at most ${max} titles per collection (${parts.join(", ")})`
      : `at most ${max} titles per collection from your caps`;
  const detail = `Min items (${minItems}) is above what your caps allow (${capPhrase}). No child collection can qualify until you lower the minimum or raise the caps.`;
  return {
    short: `Above cap (max ${max})`,
    title: detail,
  };
}