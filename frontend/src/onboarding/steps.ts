/**
 * Live product onboarding steps and guides (`/setup` + `/setup/preview`).
 *
 * Persona: media-server admin who already runs Radarr/Sonarr + Plex/Jellyfin/Emby.
 * Comfortable with libraries, disk use, and player scans. New to Placeholdarr-specific
 * ideas (pins, rematerialize, Search mode vs density). Prefer "placeholder" in copy.
 */

export type OnboardingWizardStepKey =
  | "welcome"
  | "density_search"
  | "paths"
  | "media"
  | "arr"
  | "arr_routing"
  | "behavior"
  | "look_and_feel";

export type OnboardingWizardStep = { key: OnboardingWizardStepKey; name: string };

export const ONBOARDING_WIZARD_STEPS: readonly OnboardingWizardStep[] = [
  { key: "welcome", name: "Welcome" },
  { key: "density_search", name: "Play" },
  { key: "media", name: "Players" },
  { key: "arr", name: "Arr" },
  { key: "paths", name: "Folders" },
  { key: "arr_routing", name: "Routing" },
  { key: "behavior", name: "Watching" },
  { key: "look_and_feel", name: "Appearance" },
] as const;

export type TvDensityChoice = "episode" | "season" | "series";

export const DENSITY_CHOICE_META: Record<TvDensityChoice, { label: string; summary: string }> = {
  episode: {
    label: "Every episode",
    summary: "One placeholder for each missing episode.",
  },
  season: {
    label: "One per season",
    summary: "One placeholder for each season that still needs content.",
  },
  series: {
    label: "One per show",
    summary: "One placeholder for each show that still needs content.",
  },
};

export type OnboardingStepGuideCopy = {
  title: string;
  paragraphs: string[];
};

export const ONBOARDING_STEP_GUIDES: Record<OnboardingWizardStepKey, OnboardingStepGuideCopy> = {
  welcome: {
    title: "Welcome",
    paragraphs: [],
  },
  density_search: {
    title: "How Placeholdarr should work",
    paragraphs: [
      "First, pick a starting profile for how placeholders appear in your library and what happens when you play something.",
    ],
  },
  media: {
    title: "Connect your media player",
    paragraphs: [
      "Next, let's connect the player where placeholders should show up. Choose Plex, Jellyfin, or Emby so Placeholdarr can put titles in your libraries and hear when you play something, then ask Arr to take action.",
    ],
  },
  arr: {
    title: "Connect Radarr and Sonarr",
    paragraphs: [
      "Now let's connect the Arr apps you already run. Placeholdarr follows those catalogs and creates placeholders for titles that are still missing on disk.",
    ],
  },
  paths: {
    title: "Where placeholders live",
    paragraphs: [
      "Now that your player and Arr apps are connected, let's choose where Placeholdarr should write placeholders on disk. Start with a Library Root, then create the movie and TV folders under it (Placeholdarr can create them for you).",
      "You can also map specific Arr root folders to different destinations. After those folders exist, create matching Plex libraries that point at them, then pick the libraries below.",
    ],
  },
  arr_routing: {
    title: "How Arr instances work together",
    paragraphs: [
      "Now that folders and destinations are set, choose how Placeholdarr should route playback between your Radarr and Sonarr instances.",
      "Choose shared-folder cleanup, which instance to action when a placeholder or real file plays, and whether Placeholdarr should prefer a destination that matches the played path.",
    ],
  },
  behavior: {
    title: "A few more watching options",
    paragraphs: [
      "Next, we'll cover a few miscellaneous options such as how often to sync your libraries, how future content is treated, and a few related tweaks. Defaults are fine for most setups; you can refine them later in Settings.",
    ],
  },
  look_and_feel: {
    title: "How placeholders look while downloading",
    paragraphs: [
      "Last, let's choose how placeholders look in your player while Arr is working. Status text and poster overlays show request progress. Defaults are enough to finish setup; you can refine templates in Settings anytime.",
    ],
  },
};
