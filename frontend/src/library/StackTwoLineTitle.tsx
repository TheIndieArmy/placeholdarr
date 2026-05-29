import { useLayoutEffect, useRef, useState } from "react";

const MAX_LINES = 3;
const MIN_FONT_PX = 6;
const MAX_FONT_PX = 17;
const PREFERRED_FONT_PX = 13;
const LINE_HEIGHT = 1.35;
const SLOT_BUFFER_PX = 1;

function preferredFontPx(scale: number): number {
  return Math.min(MAX_FONT_PX, Math.max(10, Math.round(PREFERRED_FONT_PX * scale)));
}

function minFontPx(scale: number, max: number): number {
  return Math.min(max, Math.max(MIN_FONT_PX, Math.round(7 * scale)));
}

function slotHeightPx(maxFontPx: number): number {
  return Math.ceil(maxFontPx * LINE_HEIGHT * MAX_LINES) + SLOT_BUFFER_PX;
}

function lineCount(el: HTMLElement, lineHeightPx: number): number {
  if (lineHeightPx <= 0) return 1;
  return Math.max(1, Math.round(el.scrollHeight / lineHeightPx));
}

function fitsWholeTitleInSlot(el: HTMLElement, width: number, px: number, slotHeight: number): boolean {
  const lineHeightPx = px * LINE_HEIGHT;
  el.style.fontSize = `${px}px`;
  el.style.lineHeight = String(LINE_HEIGHT);
  el.style.display = "block";
  el.style.width = "100%";
  el.style.maxHeight = "none";
  el.style.webkitLineClamp = "unset";
  el.style.overflow = "visible";

  if (el.scrollWidth > width + 1) return false;
  if (el.scrollHeight > slotHeight + 1) return false;
  return lineCount(el, lineHeightPx) <= MAX_LINES;
}

/** Stack card title: shrink to fit up to three centered lines; ellipsis on line 3 if still too long. */
export function StackTwoLineTitle(props: { title: string; color: string; scale: number }) {
  const max = preferredFontPx(props.scale);
  const min = minFontPx(props.scale, max);
  const slotHeight = slotHeightPx(max);

  const wrapRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const [fontPx, setFontPx] = useState(max);
  const [truncated, setTruncated] = useState(false);

  useLayoutEffect(() => {
    const wrap = wrapRef.current;
    const el = textRef.current;
    if (!wrap || !el) return;
    const width = wrap.clientWidth;
    const slot = wrap.clientHeight;
    if (width <= 0 || slot <= 0) return;

    let size = max;
    while (size > min && !fitsWholeTitleInSlot(el, width, size, slot)) {
      size -= 0.5;
    }
    const needsEllipsis = !fitsWholeTitleInSlot(el, width, size, slot);
    setFontPx(size);
    setTruncated(needsEllipsis);
  }, [props.title, min, max, props.scale]);

  return (
    <div
      ref={wrapRef}
      className="flex min-w-0 max-w-full overflow-hidden"
      style={{
        height: slotHeight,
        alignItems: truncated ? "flex-start" : "center",
      }}
    >
      <div
        ref={textRef}
        className={`w-full min-w-0 text-center font-headline font-bold uppercase tracking-wide overflow-hidden ${
          truncated ? "line-clamp-3" : ""
        }`}
        style={{
          color: props.color,
          fontSize: fontPx,
          lineHeight: LINE_HEIGHT,
          maxHeight: slotHeight,
        }}
        title={props.title}
      >
        {props.title}
      </div>
    </div>
  );
}
