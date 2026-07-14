/** @type {import('next').NextConfig} */
// METRON_WEB_BASE_PATH switches the /dash variant build (metron-ops#180): the
// second Next.js process serving metron.nousergon.ai/dash is built AND started
// with METRON_WEB_BASE_PATH=/dash. Next.js bakes basePath in at build time, so
// the variant also gets its OWN distDir (.next-dash) — both builds coexist in
// the same checkout without clobbering the primary's .next, and `next start`
// run with the same env var serves the matching output. Unset (the default,
// and the portfolio.nousergon.ai primary) leaves this config byte-identical
// to its pre-#180 shape: no basePath, default .next distDir.
const basePath = process.env.METRON_WEB_BASE_PATH || undefined;

const nextConfig = {
  reactStrictMode: true,
  ...(basePath ? { basePath, distDir: ".next-dash" } : {}),
  // Defense-in-depth for metron-ops#193: Next.js 14's built-in Server Actions
  // same-origin check compares `x-forwarded-host` against `origin` and hard-
  // rejects a mismatch. The metron-dash-web process (:3003) is reachable both
  // via its public canonical URL (metron.nousergon.ai/dash, proxied through
  // the Cloudflare Worker `metron-dash-proxy` + the internal nginx vhost) and
  // directly as metron-dash.nousergon.ai — so a legitimate browser Origin can
  // legitimately be either hostname depending on path. The ROOT-CAUSE fix is
  // at the proxy layer (metron-dash-proxy Worker + nginx preserving the
  // original X-Forwarded-Host end-to-end, tracked separately, requires live
  // infra access) — this allowlist is the documented fallback, not a
  // substitute, and must be kept in sync with any future hostname/proxy added
  // for this app.
  experimental: {
    serverActions: {
      allowedOrigins: ["metron.nousergon.ai", "metron-dash.nousergon.ai"],
    },
  },
};

export default nextConfig;
