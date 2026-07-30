// ESLint config — basePath URL guard (metron-ops#239)
//
// Next.js auto-applies basePath ONLY to <Link>, next/navigation redirect(),
// and next/image. Anything hand-built resolves against the HOST ROOT, which
// on metron.nousergon.ai is the MARKETING site, not a 404 — so an unprefixed
// URL is silently wrong, never visibly broken.
//
// Sanctioned helpers in lib/base-path.ts:
//   withBasePath("/path") — prepends the basePath for app-internal URLs
//   appUrl("/path")       — full origin + basePath for links shared externally

const NO_BAREA_HREF = "Use withBasePath() or appUrl() instead of a bare \"/...\" href on plain <a> tags. BasePath is not auto-applied — use <Link> for navigation or withBasePath()/appUrl() for hand-built URLs (lib/base-path.ts).";

const NO_BARE_FORM_ACTION = "Use appUrl() for form actions starting with \"/...\" — bare URLs skip basePath (lib/base-path.ts).";

const NO_BARE_NEXT_RESPONSE_REDIRECT = "NextResponse.redirect(new URL(\"/...\", req.url)) uses req.url's internal :3003 address, not the public origin. Do not hand-build URLs in route handlers — construct the absolute URL from the request or use a different pattern (lib/base-path.ts).";

module.exports = {
  extends: "next/core-web-vitals",

  rules: {
    "no-restricted-syntax": [
      "error",

      // <a href="/..."> — plain anchor tags don't get basePath auto-applied
      {
        selector: "JSXElement[openingElement.name.name='a'] JSXAttribute[name.name='href'][value.type='Literal'][value.value=/^\\//]",
        message: NO_BAREA_HREF,
      },

      // <form action="/..."> — form actions don't get basePath auto-applied
      {
        selector: "JSXElement[openingElement.name.name='form'] JSXAttribute[name.name='action'][value.type='Literal'][value.value=/^\\//]",
        message: NO_BARE_FORM_ACTION,
      },

      // NextResponse.redirect(new URL("/...", req.url)) — route-handler trap:
      // the first URL arg is a relative path but the origin behind the Worker→nginx
      // chain is localhost:3003, not the public metron.nousergon.ai
      {
        selector: "CallExpression[callee.object.name='NextResponse'][callee.property.name='redirect'] NewExpression[callee.name='URL'] Literal[value=/^\\//]",
        message: NO_BARE_NEXT_RESPONSE_REDIRECT,
      },
    ],
  },
};
