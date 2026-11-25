import { useLayoutEffect, useRef, useState } from "react";
import type { CollectionPreviewSampleItem, PlexSectionOption } from "../types/api";
import { useCollectionTheme } from "./collectionTheme";

const POSTER_TITLE_MAX_PX = 18;
const POSTER_TITLE_MIN_PX = 7;
const POSTER_TITLE_LINE_HEIGHT = 1.2;

export function PosterTitleFallback(props: { title: string; className?: string }) {
  const wrapRef = useRef<HTMLSpanElement>(null);
  const [fontPx, setFontPx] = useState(POSTER_TITLE_MAX_PX);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;

    const fit = () => {
      if (el.clientWidth <= 0 || el.clientHeight <= 0) return;
      let size = POSTER_TITLE_MAX_PX;
      el.style.fontSize = `${size}px`;
      while (
        size > POSTER_TITLE_MIN_PX &&
        (el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1)
      ) {
        size -= 0.5;
        el.style.fontSize = `${size}px`;
      }
      setFontPx(size);
    };

    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [props.title]);

  return (
    <span
      ref={wrapRef}
      className={`flex h-full w-full items-center justify-center overflow-hidden p-1.5 text-center break-words ${props.className ?? ""}`}
      style={{ fontSize: fontPx, lineHeight: POSTER_TITLE_LINE_HEIGHT }}
    >
      {props.title}
    </span>
  );
}

export function fileStateOutline(state: CollectionPreviewSampleItem["file_state"]): string {
  if (state === "file") return "outline outline-2 outline-offset-[-2px] outline-emerald-400";
  if (state === "placeholder") return "outline outline-2 outline-dashed outline-offset-[-2px] outline-cyan-400";
  if (state === "mixed") return "outline outline-2 outline-dashed outline-offset-[-2px] outline-violet-400";
  return "";
}

export function CatalogSamplePoster(props: {
  item: CollectionPreviewSampleItem;
  libraries: PlexSectionOption[];
  dimmed: boolean;
  showLibraryTicks: boolean;
}) {
  const theme = useCollectionTheme();
  const state = props.item.file_state ?? "none";
  const chip =
    state === "file" ? "File" : state === "placeholder" ? "PH" : state === "mixed" ? "Mix" : null;
  const inSet = new Set(props.item.in_libraries ?? []);
  return (
    <div
      title={`${props.item.title}${props.item.year ? ` (${props.item.year})` : ""}${
        chip ? ` · ${chip === "PH" ? "Placeholder" : chip === "Mix" ? "File + placeholder" : "Real file"}` : ""
      }`}
      className={`relative aspect-[2/3] w-full overflow-hidden rounded-md ${theme.posterFallback} ${fileStateOutline(state)} ${
        props.dimmed ? "opacity-35" : ""
      }`}
    >
      {props.item.poster ? (
        <img src={props.item.poster} alt={props.item.title} className="h-full w-full object-cover" loading="lazy" />
      ) : (
        <PosterTitleFallback title={props.item.title} className={theme.sampleFallback} />
      )}
      {chip ? (
        <span className="absolute left-0.5 top-0.5 rounded bg-black/70 px-1 text-[8px] font-headline uppercase tracking-wider text-white">
          {chip}
        </span>
      ) : null}
      {props.showLibraryTicks ? (
        <span className="absolute inset-x-0 bottom-0 flex flex-wrap gap-0.5 bg-black/55 px-0.5 py-0.5">
          {props.libraries.map((lib) => (
            <span
              key={lib.id}
              className={`rounded px-0.5 text-[8px] font-headline uppercase leading-none ${
                inSet.has(lib.id) ? "text-white" : "text-white/35"
              }`}
            >
              {lib.title.slice(0, 3)}
            </span>
          ))}
        </span>
      ) : null}
    </div>
  );
}
