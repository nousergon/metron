// ThemeToggle — persists the choice and toggles the `light` class on <html>, the
// contract the no-flash script in layout.tsx reads on next load.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ThemeToggle, THEME_KEY } from "@/components/theme-toggle";

describe("ThemeToggle", () => {
  // Node 25 ships a built-in localStorage that shadows jsdom's and lacks a working
  // clear(); stub a clean in-memory store per test so the component reads/writes it.
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    });
    document.documentElement.classList.remove("light");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to dark (no class, dark button pressed)", () => {
    render(<ThemeToggle />);
    expect(document.documentElement.classList.contains("light")).toBe(false);
    expect(screen.getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switching to light adds the class and persists", () => {
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button", { name: "Light" }));
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    expect(screen.getByRole("button", { name: "Light" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switching back to dark removes the class and persists", () => {
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button", { name: "Light" }));
    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    expect(document.documentElement.classList.contains("light")).toBe(false);
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("mounts to a previously saved light choice", () => {
    localStorage.setItem(THEME_KEY, "light");
    render(<ThemeToggle />);
    expect(screen.getByRole("button", { name: "Light" })).toHaveAttribute("aria-pressed", "true");
  });
});
