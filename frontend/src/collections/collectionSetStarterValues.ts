import type { CollectionSetConfig } from "../types/api";
import { resolvePresetNames } from "./discoverStarterSets";

type SetCategory = CollectionSetConfig["category"];

const MOVIE_GENRE_STARTERS = ["Action", "Comedy", "Drama", "Horror", "Science Fiction"];
const SHOW_GENRE_STARTERS = ["Drama", "Comedy", "Documentary", "Crime", "Sci-Fi & Fantasy"];

const MOVIE_STUDIO_STARTERS = [
  "Warner Bros. Pictures",
  "Universal Pictures",
  "Walt Disney Pictures",
];

const TV_NETWORK_STARTERS = ["HBO", "Netflix", "Apple TV+", "Amazon", "Disney+"];

const DECADE_STARTERS = ["2010s", "2000s", "1990s"];

const MOVIE_RATING_STARTERS = ["PG-13", "R", "PG", "G"];
const SHOW_RATING_STARTERS = ["TV-MA", "TV-14", "TV-PG", "TV-G"];

const RELEASE_TIMING_STARTERS = ["upcoming", "this_month"];

/** TMDB movie genre names (picker fallback before API / library). */
const PICKER_FALLBACK_MOVIE_GENRES = [
  "Action",
  "Adventure",
  "Animation",
  "Comedy",
  "Crime",
  "Documentary",
  "Drama",
  "Family",
  "Fantasy",
  "History",
  "Horror",
  "Music",
  "Mystery",
  "Romance",
  "Science Fiction",
  "Thriller",
  "TV Movie",
  "War",
  "Western",
];

const PICKER_FALLBACK_TV_GENRES = [
  "Action & Adventure",
  "Animation",
  "Comedy",
  "Crime",
  "Documentary",
  "Drama",
  "Family",
  "Kids",
  "Mystery",
  "News",
  "Reality",
  "Sci-Fi & Fantasy",
  "Soap",
  "Talk",
  "War & Politics",
  "Western",
];

const PICKER_FALLBACK_STUDIOS = [
  "Warner Bros. Pictures",
  "Universal Pictures",
  "Paramount Pictures",
  "Walt Disney Pictures",
  "Sony Pictures",
  "20th Century Studios",
  "Lionsgate",
  "A24",
  "Metro-Goldwyn-Mayer",
  "DreamWorks Pictures",
  "Columbia Pictures",
  "Marvel Studios",
  "Lucasfilm",
  "Pixar",
  "Legendary Pictures",
  "Studio Ghibli",
  "Focus Features",
  "Neon",
];

const PICKER_FALLBACK_NETWORKS = [
  "HBO",
  "Netflix",
  "Apple TV+",
  "Amazon",
  "Prime Video",
  "Disney+",
  "Hulu",
  "Paramount+",
  "Peacock",
  "AMC",
  "FX",
  "BBC One",
  "National Geographic",
  "NBC",
  "CBS",
  "ABC",
  "FOX",
  "Showtime",
  "Adult Swim",
];

/** Full picker pool when builder-meta has not loaded yet (no Plex library required). */
export function pickerOptionsForCategory(
  category: SetCategory,
  sectionType: "movie" | "show",
): string[] {
  switch (category) {
    case "genre":
      return sectionType === "movie" ? PICKER_FALLBACK_MOVIE_GENRES : PICKER_FALLBACK_TV_GENRES;
    case "studio":
      return PICKER_FALLBACK_STUDIOS;
    case "network":
      return PICKER_FALLBACK_NETWORKS;
    case "decade":
      return [
        "2020s",
        "2010s",
        "2000s",
        "1990s",
        "1980s",
        "1970s",
        "1960s",
        "1950s",
      ];
    case "content_rating":
      return sectionType === "movie"
        ? ["G", "PG", "PG-13", "R", "NC-17", "NR"]
        : ["TV-G", "TV-PG", "TV-14", "TV-MA", "NR"];
    case "release_timing":
      return ["upcoming", "this_week", "this_month", "this_year", "this_decade"];
  case "tag":
      return [];
    default:
      return [];
  }
}

/** Example picks for new Collection Sets (include mode). */
export function starterValuesForCategory(
  category: SetCategory,
  sectionType: "movie" | "show",
  catalogOptions?: string[],
): string[] {
  let raw: string[] = [];
  switch (category) {
    case "genre":
      raw = sectionType === "movie" ? MOVIE_GENRE_STARTERS : SHOW_GENRE_STARTERS;
      break;
    case "studio":
      raw = MOVIE_STUDIO_STARTERS;
      break;
    case "network":
      raw = TV_NETWORK_STARTERS;
      break;
    case "decade":
      raw = DECADE_STARTERS;
      break;
    case "content_rating":
      raw = sectionType === "movie" ? MOVIE_RATING_STARTERS : SHOW_RATING_STARTERS;
      break;
    case "release_timing":
      raw = RELEASE_TIMING_STARTERS;
      break;
    case "tag":
      return [];
    default:
      return [];
  }
  if (catalogOptions?.length) {
    return resolvePresetNames(raw, catalogOptions);
  }
  return raw;
}