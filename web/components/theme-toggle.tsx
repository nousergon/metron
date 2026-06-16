"use client";

// Light/dark theme toggle (metron-ops#50). Persists to localStorage and toggles the
// `light` class on <html>; the no-flash script in app/layout.tsx applies the saved
// choice before first paint. Dark is the default. Mounts to the saved value on the
// client to avoid a hydration mismatch (server always renders the default).

import { useEffect, useState } from "react";

export type Theme = "dark" | "light";
export const THEME_KEY = "metron-theme";

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("light", theme === "light");
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Private mode / storage disabled — the class still applies for this session.
  }
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    let saved: Theme = "dark";
    try {
      if (localStorage.getItem(THEME_KEY) === "light") saved = "light";
    } catch {
      // ignore
    }
    setTheme(saved);
    setMounted(true);
  }, []);

  function choose(next: Theme) {
    setTheme(next);
    applyTheme(next);
  }

  const options: { value: Theme; label: string }[] = [
    { value: "dark", label: "Dark" },
    { value: "light", label: "Light" },
  ];

  return (
    <div className="inline-flex rounded-lg border border-line bg-surface p-1" role="group" aria-label="Theme">
      {options.map((opt) => {
        const active = mounted && theme === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            aria-pressed={active}
            onClick={() => choose(opt.value)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
              active ? "bg-paper text-ink" : "text-muted hover:text-ink"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
