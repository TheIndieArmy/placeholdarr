/** Required TMDB API legal notice — https://developer.themoviedb.org/docs/faq */
export const TMDB_API_NOTICE =
  "This product uses the TMDB API but is not endorsed or certified by TMDB.";

export function isTmdbPosterUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  return /(?:^|\/\/)(?:image\.)?tmdb\.org/i.test(url) || url.includes("themoviedb.org");
}

export function definitionUsesTmdbSources(definition?: { sources?: { type: string }[] }): boolean {
  return (definition?.sources ?? []).some((source) => source.type.startsWith("tmdb_"));
}
