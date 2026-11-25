import type { ThemeMode } from "../../brandTypes";
import { DiscoverSetupPanel } from "../DiscoverSetupPanel";

/** Discover setup using the next Collections shell (preview rail wired in DiscoverSetupPanel). */
export function DiscoverBuilder(props: {
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
  onDone: () => void;
  onOpenPlexSettings?: () => void;
  onOpenOrdering?: () => void;
}) {
  return <DiscoverSetupPanel {...props} uiVariant="next" />;
}