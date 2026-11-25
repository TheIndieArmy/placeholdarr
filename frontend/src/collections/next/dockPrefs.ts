import { useCallback, useEffect, useState } from "react";

export type DockPrefs = {
  minimized: boolean;
  ghosts: boolean;
  animate: boolean;
  plexMock: boolean;
};

const KEY = "placeholdarr.collectionsDock";
const DEFAULTS: DockPrefs = { minimized: false, ghosts: true, animate: true, plexMock: true };
const listeners = new Set<(prefs: DockPrefs) => void>();

function read(): DockPrefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as Partial<DockPrefs>) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

/** Preview dock display options, shared by every builder and persisted per browser. */
export function useDockPrefs(): [DockPrefs, (patch: Partial<DockPrefs>) => void] {
  const [prefs, setPrefs] = useState<DockPrefs>(read);

  useEffect(() => {
    listeners.add(setPrefs);
    return () => {
      listeners.delete(setPrefs);
    };
  }, []);

  const update = useCallback((patch: Partial<DockPrefs>) => {
    const next = { ...read(), ...patch };
    try {
      window.localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* private mode */
    }
    listeners.forEach((fn) => fn(next));
  }, []);

  return [prefs, update];
}
