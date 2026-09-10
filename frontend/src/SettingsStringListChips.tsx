import { useState, type KeyboardEvent } from "react";
import { normalizeArrTagLabel } from "./api/collections";

function parseStringListValue(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw.map((item) => String(item ?? "").trim()).filter(Boolean);
  }
  if (typeof raw === "string") {
    const text = raw.trim();
    if (!text) return [];
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        return parsed.map((item) => String(item ?? "").trim()).filter(Boolean);
      }
    } catch {
      /* fall through */
    }
    return text
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
  }
  return [];
}

export function SettingsStringListChips(props: {
  value: unknown;
  onChange: (next: string[]) => void;
  disabled?: boolean;
  focusClass: string;
  placeholder?: string;
}) {
  const tags = parseStringListValue(props.value);
  const [draft, setDraft] = useState("");

  function commitDraft() {
    const label = normalizeArrTagLabel(draft);
    setDraft("");
    if (!label) return;
    const key = label.toLowerCase();
    if (tags.some((tag) => tag.toLowerCase() === key)) return;
    props.onChange([...tags, label]);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      commitDraft();
      return;
    }
    if (event.key === "Backspace" && !draft && tags.length) {
      props.onChange(tags.slice(0, -1));
    }
  }

  return (
    <div className="space-y-1.5 max-w-xl">
      <div
        className={`flex min-h-[2.5rem] flex-wrap items-center gap-1.5 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-2 py-1.5 ${
          props.disabled ? "opacity-50" : ""
        }`}
      >
        {tags.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded-md border border-[#424753]/50 bg-[#252e3a]/80 px-1.5 py-0.5 text-[13px] text-slate-200"
          >
            {tag}
            <button
              type="button"
              disabled={props.disabled}
              className="leading-none text-slate-400 hover:text-white disabled:cursor-not-allowed"
              aria-label={`Remove ${tag}`}
              onClick={() => props.onChange(tags.filter((item) => item !== tag))}
            >
              ×
            </button>
          </span>
        ))}
        <input
          className={`min-w-[8rem] flex-1 bg-transparent text-[16px] text-slate-200 outline-none ${props.focusClass}`}
          value={draft}
          disabled={props.disabled}
          placeholder={
            props.placeholder ||
            (tags.length ? "Add another, then Enter" : "Type a tag and press Enter")
          }
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          onBlur={commitDraft}
        />
      </div>
      <p className="ui-field-description-compact text-slate-500">
        Press Enter to add. Spaces become dashes, like Arr tags.
      </p>
    </div>
  );
}
