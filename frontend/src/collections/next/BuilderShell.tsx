import type { ReactNode } from "react";

/** Full-width builder column with the preview dock pinned below and the full-preview sheet on demand. */
export function BuilderShell(props: { config: ReactNode; dock: ReactNode; sheet?: ReactNode }) {
  return (
    <div className="relative pb-60">
      <div className="mx-auto flex w-full max-w-5xl min-w-0 flex-col gap-3">{props.config}</div>
      {props.dock}
      {props.sheet}
    </div>
  );
}
