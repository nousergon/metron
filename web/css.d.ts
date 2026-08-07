// Ambient declaration for global (non-module) CSS imports such as
// `import "./globals.css"` in app/layout.tsx. Next.js handles these at build
// time but ships type declarations only for `*.module.css`; TypeScript 6+
// checks side-effect imports (TS2882) and needs this module to resolve.
declare module "*.css";
