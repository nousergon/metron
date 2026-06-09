import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Restrained, measured palette — "portfolio analytics, measured".
        ink: "#0f172a",
        muted: "#64748b",
        line: "#e2e8f0",
        positive: "#15803d",
        negative: "#b91c1c",
      },
    },
  },
  plugins: [],
};

export default config;
