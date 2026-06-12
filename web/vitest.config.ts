import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true, // enables @testing-library/react auto-cleanup between tests
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["__tests__/**/*.test.{ts,tsx}"],
  },
  resolve: {
    // Mirror tsconfig's `@/*` path alias so components import identically in tests.
    alias: { "@": path.resolve(__dirname, ".") },
  },
});
