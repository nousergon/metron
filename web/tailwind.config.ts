import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Semantic brand tokens resolve through CSS variables (RGB channels) so the
        // light/dark theme toggle can re-point them at runtime (globals.css holds the
        // dark default + `.light` overrides). Components style through these tokens;
        // literal Tailwind colors stay out of components. The `<alpha-value>` form keeps
        // Tailwind's opacity modifiers (e.g. `bg-surface/50`) working.
        paper: "rgb(var(--c-paper) / <alpha-value>)",       // page background
        surface: "rgb(var(--c-surface) / <alpha-value>)",   // raised surfaces: cards, table heads, inputs
        ink: "rgb(var(--c-ink) / <alpha-value>)",           // primary text
        muted: "rgb(var(--c-muted) / <alpha-value>)",       // secondary text
        line: "rgb(var(--c-line) / <alpha-value>)",         // borders
        positive: "rgb(var(--c-positive) / <alpha-value>)", // P&L green
        negative: "rgb(var(--c-negative) / <alpha-value>)", // P&L red
        accent: "rgb(var(--c-accent) / <alpha-value>)",     // links/highlights
      },
    },
  },
  plugins: [],
};

export default config;
