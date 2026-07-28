// Single source of truth for the deployed basePath in code that builds a URL BY HAND.
//
// next.config.mjs bakes Next's own `basePath` from METRON_WEB_BASE_PATH at build time
// and re-exports the identical value as NEXT_PUBLIC_BASE_PATH, so nothing here has to
// hardcode a second copy of "/dash" that can drift from the one Next is serving.
//
// WHY THIS EXISTS (metron#298 fallout): Next.js only auto-prefixes basePath for
// <Link>, next/navigation's redirect(), and next/image. Anything hand-built does NOT
// get the prefix and silently resolves against the HOST ROOT:
//
//   - an absolute callbackURL handed to the shared auth service
//   - a plain <a href="/..."> or <form action="/...">
//   - a Location header emitted by a route handler
//
// On metron.nousergon.ai the host root is the MARKETING site, not the app — so an
// unprefixed link doesn't 404 (which would have been noticed immediately), it lands
// the user on a plausible-looking marketing page. That is exactly how the magic-link
// sign-in appeared broken: the session cookie was set correctly and the user was
// genuinely signed in, but the post-verify redirect dropped them on the marketing
// landing page with no signed-in affordance.
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

/**
 * Prefix an app-absolute path with the deployed basePath:
 * `/login` → `/dash/login` in the /dash build, `/login` when unset (local dev).
 */
export function withBasePath(path: string): string {
  if (!path.startsWith("/")) {
    throw new Error(`withBasePath expects an app-absolute path (leading "/"), got: ${path}`);
  }
  // Keep the app root free of a trailing slash ("/dash", not "/dash/") so the value is
  // byte-identical to the canonical URL Next serves and no redirect hop is introduced.
  if (path === "/") return BASE_PATH || "/";
  return `${BASE_PATH}${path}`;
}

/**
 * Fully-qualified URL for an app path — for handing to an EXTERNAL service that has to
 * redirect the browser back into the app (the magic-link callbackURL is the only such
 * case today). Browser-only: reads window.location.origin, so the deployed origin is
 * whatever the user actually reached us on rather than a second configured constant.
 */
export function appUrl(path: string): string {
  if (typeof window === "undefined") {
    throw new Error("appUrl() is browser-only — it reads window.location.origin.");
  }
  return `${window.location.origin}${withBasePath(path)}`;
}
