import { BrandSelect } from "../../BrandSelect";
import type { ThemeMode } from "../../brandTypes";
import type { CollectionPreviewResponse } from "../../types/api";
import { getCollectionTheme } from "../collectionTheme";

type ChildRow = NonNullable<CollectionPreviewResponse["set_collections"]>[number];

/** Dock control to step through the collections a set would build. */
export function ChildSwitcher(props: {
  themeMode: ThemeMode;
  rows: ChildRow[];
  selectedTitle: string | null;
  onSelect: (title: string) => void;
}) {
  const theme = getCollectionTheme(props.themeMode === "light");
  if (!props.rows.length) return null;
  const index = Math.max(
    0,
    props.rows.findIndex((r) => r.title === props.selectedTitle),
  );
  const step = (delta: number) => {
    const next = props.rows[(index + delta + props.rows.length) % props.rows.length];
    if (next) props.onSelect(next.title);
  };
  return (
    <>
      <button
        type="button"
        onClick={() => step(-1)}
        className={`material-symbols-outlined rounded p-1 ${theme.iconAction}`}
        style={{ fontSize: 18 }}
        aria-label="Previous collection"
        title="Previous collection"
      >
        chevron_left
      </button>
      <BrandSelect
        triggerClassName="min-w-[12rem] max-w-[18rem]"
        value={props.rows[index]?.title ?? ""}
        options={props.rows.map((r) => ({
          value: r.title,
          label: `${r.title}${r.skipped === "below_min" ? " (below min)" : ""}`,
        }))}
        onChange={props.onSelect}
        ariaLabel="Collection to preview"
      />
      <button
        type="button"
        onClick={() => step(1)}
        className={`material-symbols-outlined rounded p-1 ${theme.iconAction}`}
        style={{ fontSize: 18 }}
        aria-label="Next collection"
        title="Next collection"
      >
        chevron_right
      </button>
      <span className={`ml-1 shrink-0 text-[12px] ${theme.muted}`}>
        {index + 1} of {props.rows.length}
      </span>
    </>
  );
}
