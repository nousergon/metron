import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Dark brand palette — matches the Nous Ergon site (black, zinc text,
        // hairline borders, restrained accents). Components style through these
        // semantic tokens; literal Tailwind colors stay out of components.
        paper: "#09090b",    // page background (zinc-950)
        surface: "#131316",  // raised surfaces: cards, table heads, inputs
        ink: "#f4f4f5",      // primary text (zinc-100)
        muted: "#a1a1aa",    // secondary text (zinc-400)
        line: "#27272a",     // borders (zinc-800)
        positive: "#34d399", // P&L green tuned for dark (emerald-400)
        negative: "#fb7185", // P&L red tuned for dark (rose-400)
        accent: "#38bdf8",   // links/highlights (sky-400, the site accent)
      },
    },
  },
  plugins: [],
};

export default config;
