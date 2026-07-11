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
};

export default nextConfig;
