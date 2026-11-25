import { useMemo } from "react";
import type { CollectionAutoAddConfig } from "../types/api";
import type { ThemeMode } from "../brandTypes";
import { getCollectionTheme } from "./collectionTheme";
import { ToggleSwitch } from "./ToggleSwitch";
import { CollectionAutoAddSection } from "./CollectionAutoAddSection";

function ShelfNumberInput(props: {
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  placeholder?: string;
  min?: number;
  max?: number;
  fieldClassName: string;
  disabled?: boolean;
}) {
  return (
    <input
      type="number"
      className={props.fieldClassName}
      style={{ width: 90 }}
      disabled={props.disabled}
      value={props.value ?? ""}
      min={props.min}
      max={props.max}
      placeholder={props.placeholder}
      onChange={(e) => {
        const raw = e.target.value;
        if (raw === "") {
          props.onChange(null);
          return;
        }
        const parsed = Number(raw);
        props.onChange(Number.isFinite(parsed) ? parsed : null);
      }}
    />
  );
}

export function CollectionShelfContentsSection(props: {
  themeMode: ThemeMode;
  accentHex: string;
  mediaType: "movie" | "show";
  includeLibrary: boolean;
  libraryLimit: number | null | undefined;
  onIncludeLibraryChange: (next: boolean) => void;
  onLibraryLimitChange: (next: number | null) => void;
  autoAdd: CollectionAutoAddConfig | null | undefined;
  onAutoAddChange: (next: CollectionAutoAddConfig | null | undefined) => void;
  scopeHint?: string;
  disabledReason?: string;
  layout?: "section" | "panel";
  /** When false, Arr target pickers stay hidden until a Plex library is chosen. */
  arrTargetsReady?: boolean;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const layout = props.layout ?? "section";
  const nonLibraryOn = Boolean(props.autoAdd?.enabled);
  const libraryCap = props.libraryLimit ?? null;
  const missingCap = props.autoAdd?.missing_limit ?? props.autoAdd?.max_per_run ?? 10;

  const summary = useMemo(() => {
    if (!props.includeLibrary && !nonLibraryOn) {
      return "Turn on at least one source below.";
    }
    const parts: string[] = [];
    if (props.includeLibrary) {
      parts.push(
        libraryCap != null && libraryCap > 0
          ? `up to ${libraryCap} from your library`
          : "all matching library titles",
      );
    }
    if (nonLibraryOn) {
      parts.push(`up to ${missingCap} not in your library`);
    }
    return parts.length ? `Each collection: ${parts.join(", ")}.` : null;
  }, [libraryCap, missingCap, nonLibraryOn, props.includeLibrary]);

  const bothOff = !props.includeLibrary && !nonLibraryOn;
  return (
    <div className={layout === "section" ? `border-t pt-4 ${theme.divider}` : "space-y-4"}>
      {layout === "section" ? (
        <div className={`mb-2 ${theme.blockTitle}`}>What&apos;s in each collection</div>
      ) : null}
      {props.scopeHint ? <p className={`text-[13px] ${theme.muted}`}>{props.scopeHint}</p> : null}
      {props.disabledReason ? (
        <p className={`text-[13px] ${isLight ? "text-amber-800" : "text-amber-300"}`}>{props.disabledReason}</p>
      ) : null}

      <div className="space-y-3">
        <label className={`flex flex-wrap items-center gap-3 text-[13px] ${theme.label}`}>
          <ToggleSwitch
            checked={props.includeLibrary}
            accentHex={props.accentHex}
            ariaLabel="Include titles in your library"
            disabled={Boolean(props.disabledReason)}
            onChange={props.onIncludeLibraryChange}
          />
          <span className="shrink-0">Titles in your library</span>
          {props.includeLibrary ? (
            <>
              <span className={theme.muted}>up to</span>
              <ShelfNumberInput
                fieldClassName={theme.field}
                value={props.libraryLimit ?? null}
                min={1}
                max={500}
                placeholder="No limit"
                onChange={props.onLibraryLimitChange}
              />
            </>
          ) : null}
        </label>

        <CollectionAutoAddSection
          layout="panel"
          themeMode={props.themeMode}
          accentHex={props.accentHex}
          mediaType={props.mediaType}
          autoAdd={props.autoAdd}
          onChange={props.onAutoAddChange}
          disabledReason={props.disabledReason}
          hideHeader
          arrTargetsReady={props.arrTargetsReady}
        />
      </div>

      {summary ? (
        <p
          className={`text-[13px] ${bothOff ? (isLight ? "text-amber-800" : "text-amber-300") : theme.muted}`}
        >
          {summary}
        </p>
      ) : null}
    </div>
  );
}