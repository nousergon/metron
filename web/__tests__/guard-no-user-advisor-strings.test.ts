import { readFileSync, readdirSync } from "fs";
import { join } from "path";

const USER_FACING_FILES = [
  "web/components/**/*.tsx",
  "web/app/portfolios/*/page.tsx",
  "web/app/portfolios/*/*/page.tsx",
];

// Internal identifiers that are allowed to contain "advisor" — the rename only touches
// user-facing copy; type names, component filenames, API routes, and the preserved
// /advisor redirect route stay as-is (metron-ops#165).
const ALLOWLIST = /(metron_ext\.advisor|ai_advisor|\/metron\/llm\/advisor|advisor-profile-form|generate-advisor|["'/]advisor["'\/]|\/advisor\b)/i;

describe("Guard: no user-facing 'Advisor' strings (metron-ops#165)", () => {
  it("should have no case-insensitive 'advisor' or 'Advisor' in user-facing strings", () => {
    const webDir = join(process.cwd(), "web");
    const issues: Array<{ file: string; line: number; content: string }> = [];

    function scanDir(dir: string) {
      try {
        const entries = readdirSync(dir, { withFileTypes: true });
        for (const entry of entries) {
          if (entry.isDirectory()) {
            // Skip node_modules and .next
            if (entry.name === "node_modules" || entry.name === ".next") continue;
            scanDir(join(dir, entry.name));
          } else if (entry.name.endsWith(".tsx")) {
            const filePath = join(dir, entry.name);
            const content = readFileSync(filePath, "utf-8");
            const lines = content.split("\n");
            lines.forEach((line, idx) => {
              // Match "advisor" or "Advisor" case-insensitively, but exclude allowlisted internal identifiers
              if (/\b(advisor|Advisor)\b/i.test(line) && !ALLOWLIST.test(line)) {
                issues.push({
                  file: filePath.replace(process.cwd(), ""),
                  line: idx + 1,
                  content: line.trim(),
                });
              }
            });
          }
        }
      } catch {
        // Ignore unreadable directories
      }
    }

    scanDir(webDir);

    if (issues.length > 0) {
      const report = issues
        .map(
          (issue) =>
            `  ${issue.file}:${issue.line}: ${issue.content.substring(0, 80)}`
        )
        .join("\n");
      throw new Error(
        `Found user-facing "advisor" strings (should be "Intelligence"). Update or add to ALLOWLIST if internal:\n${report}`
      );
    }
  });
});
