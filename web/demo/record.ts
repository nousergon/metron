/**
 * Record the 60-second Metron demo as video, unattended (metron-ops-I305 deliverable 2).
 *
 * Drives the real pages in a headless Chromium at 390x844 with Playwright's recorder on,
 * following `metron-ops/docs/runbooks/metron-demo-path.md` §1 step for step, so the video
 * and the runbook cannot drift: the step list below IS the runbook's tap path, and a
 * caption bar carries the sentence the runbook says to say.
 *
 * One command:
 *
 *   npm --prefix web run demo:record
 *
 * with `BASE_URL` and `METRON_STORAGE_STATE` set (see below). Output lands in
 * `web/demo/out/` — `metron-demo.webm` plus `metron-demo.json` (per-step timings, the
 * build commit, the mask verdict). `web/demo/out/` is gitignored: a recording is an
 * artifact of the commit it was made from, not a source file.
 *
 * Environment:
 *   BASE_URL              Origin + basePath of the build to record, e.g.
 *                         http://localhost:3000 for `npm run dev`, or the deployed
 *                         owner build's `/dash` origin. No host is hard-coded here —
 *                         this repo is public.
 *   METRON_STORAGE_STATE  Path to a Playwright storageState JSON carrying an
 *                         authenticated session. REQUIRED: the demo path is behind
 *                         login, and this script never handles credentials itself.
 *                         Produce one with `npx playwright open --save-storage=<path>`,
 *                         signing in by hand once.
 *   HOUSEHOLD             Portfolio/household id to walk. Defaults to the seeded demo
 *                         household, which is what the runbook says to record on.
 *   PACE                  Scales every pause (PACE=0.5 -> half as long). Default 1.
 *   MASK                  "1" (default) records with ?demo_mask=1, so every currency
 *                         amount renders as a percentage. "0" records real amounts and
 *                         is legal ONLY on the seeded demo household.
 *
 * Fail-loud properties, because this output gets published:
 *   - No storage state, or a session that lands back on /login -> raise, no video kept.
 *   - Mask on but a page still shows a currency amount -> raise, naming the offending
 *     strings. A masked recording that leaked one cell is worse than no recording.
 *   - Any step whose page never reaches its expected marker -> raise.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { type Browser, type BrowserContext, type Page, chromium } from "playwright";
import { currencyPattern } from "../lib/demo-mask";

const BASE_URL = (process.env.BASE_URL ?? "http://localhost:3000").replace(/\/+$/, "");
const STORAGE_STATE = process.env.METRON_STORAGE_STATE ?? "";
// The seeded demo household (api/services/demo_household.py). The runbook records here.
const HOUSEHOLD = process.env.HOUSEHOLD ?? "00000000-0000-0000-0000-00000000de63";
const PACE = Number(process.env.PACE ?? "1");
const MASK = (process.env.MASK ?? "1") === "1";

const OUT_DIR = path.join(__dirname, "out");
const VIEWPORT = { width: 390, height: 844 };

interface Step {
  /** Runbook step number — the two lists must stay in lockstep. */
  n: number;
  name: string;
  /** Path under BASE_URL. */
  href: string;
  /** Text that must appear before the step counts as reached. */
  marker: RegExp;
  /** The one sentence from the runbook, shown as a caption. */
  say: string;
  /** Seconds to dwell once the marker is up. */
  dwell: number;
}

const STEPS: Step[] = [
  {
    n: 1,
    name: "glance",
    href: `/portfolios/${HOUSEHOLD}/glance`,
    marker: /reconciled|as of|glance/i,
    say: "This is the one screen I'd check. Every number says how fresh it is.",
    dwell: 12,
  },
  {
    n: 2,
    name: "goal",
    href: `/portfolios/${HOUSEHOLD}/overview`,
    marker: /goal|progress/i,
    say: "It measures your real accounts against the number you chose. It doesn't project or advise.",
    dwell: 9,
  },
  {
    n: 3,
    name: "tax",
    href: `/portfolios/${HOUSEHOLD}/tax`,
    marker: /lot|long-term|wash/i,
    say: "US lot-level tax on linked accounts — not an Australian tax tracker.",
    dwell: 9,
  },
  {
    n: 4,
    name: "plan",
    href: `/portfolios/${HOUSEHOLD}/plan`,
    marker: /target|cash/i,
    say: "You set the targets. Metron does the arithmetic and never picks the stocks.",
    dwell: 10,
  },
  {
    n: 5,
    name: "market",
    href: `/portfolios/${HOUSEHOLD}/market`,
    marker: /attractiveness|held|watchlist/i,
    say: "A daily read on price action, with its track record published. It is not a forecast.",
    dwell: 10,
  },
  {
    n: 6,
    name: "attribution",
    href: `/portfolios/${HOUSEHOLD}/attribution`,
    marker: /allocation|selection|attribution/i,
    say: "The depth underneath: attribution and factor risk, which neither tracker has.",
    dwell: 10,
  },
];

function pause(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, seconds * PACE * 1000)));
}

function stepUrl(step: Step): string {
  const q = MASK ? "?demo_mask=1" : "";
  return `${BASE_URL}${step.href}${q}`;
}

/** The caption bar is injected by this script — it is not part of the product. */
async function caption(page: Page, text: string): Promise<void> {
  await page.evaluate((message) => {
    const id = "metron-demo-caption";
    let bar = document.getElementById(id);
    if (!bar) {
      bar = document.createElement("div");
      bar.id = id;
      bar.setAttribute(
        "style",
        [
          "position:fixed",
          "left:0;right:0;bottom:0;z-index:2147483647",
          "padding:14px 16px",
          "background:rgba(10,10,12,0.92)",
          "color:#fff",
          "font:500 15px/1.35 system-ui,-apple-system,sans-serif",
          "text-align:center",
        ].join(";"),
      );
      document.body.appendChild(bar);
    }
    bar.textContent = message;
  }, text);
}

/** Every string on the page that still shows a currency amount. */
async function residualCurrency(page: Page): Promise<string[]> {
  const source = currencyPattern().source;
  return page.evaluate((patternSource) => {
    const re = new RegExp(patternSource, "g");
    const hits: string[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const parent = (node as Text).parentElement;
      const tag = parent?.tagName ?? "";
      if (tag !== "SCRIPT" && tag !== "STYLE" && tag !== "NOSCRIPT") {
        re.lastIndex = 0;
        if (re.test((node as Text).data)) hits.push((node as Text).data.trim());
      }
      node = walker.nextNode();
    }
    return hits;
  }, source);
}

async function runStep(page: Page, step: Step): Promise<{ step: number; name: string; seconds: number }> {
  const started = Date.now();
  await page.goto(stepUrl(step), { waitUntil: "domcontentloaded" });

  if (/\/login/.test(new URL(page.url()).pathname)) {
    throw new Error(
      `step ${step.n} (${step.name}) redirected to the login page — METRON_STORAGE_STATE is missing, expired or for a different origin.`,
    );
  }

  if (MASK) {
    // Block until the mask has run: the recorder must never film a pre-mask frame.
    await page.waitForSelector("html[data-demo-mask-applied='1']", { timeout: 15_000 });
  }

  await page.waitForFunction(
    (pattern) => new RegExp(pattern, "i").test(document.body.innerText),
    step.marker.source,
    { timeout: 20_000 },
  );

  if (MASK) {
    const leaked = await residualCurrency(page);
    if (leaked.length > 0) {
      throw new Error(
        `step ${step.n} (${step.name}) still renders currency with ?demo_mask=1 — the mask does not cover it: ${JSON.stringify(leaked.slice(0, 10))}`,
      );
    }
  }

  await caption(page, `${step.n}. ${step.say}`);
  await pause(step.dwell);
  return { step: step.n, name: step.name, seconds: (Date.now() - started) / 1000 };
}

function buildCommit(): string {
  try {
    return execFileSync("git", ["rev-parse", "--short", "HEAD"], { cwd: __dirname }).toString().trim();
  } catch {
    return "unknown";
  }
}

async function main(): Promise<void> {
  if (!STORAGE_STATE) {
    throw new Error(
      "METRON_STORAGE_STATE is required — the demo path is behind login. Produce one with `npx playwright open --save-storage=<path>` and sign in once.",
    );
  }
  if (!existsSync(STORAGE_STATE)) {
    throw new Error(`METRON_STORAGE_STATE points at a file that does not exist: ${STORAGE_STATE}`);
  }

  rmSync(OUT_DIR, { recursive: true, force: true });
  mkdirSync(OUT_DIR, { recursive: true });

  const browser: Browser = await chromium.launch();
  const context: BrowserContext = await browser.newContext({
    storageState: STORAGE_STATE,
    viewport: VIEWPORT,
    deviceScaleFactor: 3,
    isMobile: true,
    hasTouch: true,
    recordVideo: { dir: OUT_DIR, size: VIEWPORT },
  });
  const page = await context.newPage();

  const timings: { step: number; name: string; seconds: number }[] = [];
  let video: Awaited<ReturnType<Page["video"]>> = null;
  try {
    for (const step of STEPS) timings.push(await runStep(page, step));
  } finally {
    video = page.video();
    await context.close(); // flushes the video file
    await browser.close();
    if (video) {
      const src = await video.path();
      renameSync(src, path.join(OUT_DIR, "metron-demo.webm"));
    }
  }

  const total = timings.reduce((a, t) => a + t.seconds, 0);
  writeFileSync(
    path.join(OUT_DIR, "metron-demo.json"),
    `${JSON.stringify(
      {
        recorded_at: new Date().toISOString(),
        base_url: BASE_URL,
        household: HOUSEHOLD,
        commit: buildCommit(),
        masked: MASK,
        viewport: VIEWPORT,
        total_seconds: Number(total.toFixed(1)),
        steps: timings,
      },
      null,
      2,
    )}\n`,
  );

  console.log(`recorded ${timings.length} steps in ${total.toFixed(1)}s -> ${path.join(OUT_DIR, "metron-demo.webm")}`);
  if (total > 60) {
    console.warn(`WARNING: the path took ${total.toFixed(1)}s — the runbook budgets 60s. Lower PACE or trim a dwell.`);
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
