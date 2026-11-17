import type { NextConfig } from "next";

/**
 * The browser only ever talks to this Next.js server. Everything under /api is
 * forwarded to the FastAPI backend, so the session cookie stays same-origin and
 * the backend address never reaches client code.
 *
 * API_ORIGIN is read on the server only. Rewrite destinations are resolved when
 * the app is built (they are written into the routes manifest), so a production
 * build must be made with the API_ORIGIN it will run against. The Dockerfile
 * takes it as a build argument for that reason.
 */
const API_ORIGIN = (process.env.API_ORIGIN ?? "http://127.0.0.1:8010").replace(/\/+$/, "");

/**
 * Content Security Policy for the production build. Everything the app needs is
 * served from its own origin (scripts, styles, self-hosted fonts, the /api
 * proxy), so nothing else is allowed to load, to be called, or to frame it.
 * Inline scripts and styles stay allowed: Next.js streams its page data in
 * inline scripts, and the pages are prerendered, so there is no per-request
 * nonce to give them. The development server needs eval and a websocket for
 * hot reloading, so the policy is only sent by production builds.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  // Employer sites opened from a job never learn which page the visitor came from.
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  ...(process.env.NODE_ENV === "production" ? [{ key: "Content-Security-Policy", value: contentSecurityPolicy }] : []),
];

const nextConfig: NextConfig = {
  output: "standalone",
  // A second build can live beside the main one (the browser tests build against the demo API into .next-e2e).
  distDir: process.env.NEXT_DIST_DIR || ".next",
  poweredByHeader: false,
  cacheComponents: true,
  partialPrefetching: true,
  // The dev server is opened as http://127.0.0.1:<port> as well as http://localhost:<port>.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // Only read by src/instrumentation.ts (server start-up) to warn when the runtime API_ORIGIN
  // differs from the one this build forwards /api to. No client code refers to it.
  env: { BUILT_API_ORIGIN: API_ORIGIN },
  // The development badge would sit on top of the sidebar and the mobile layout.
  devIndicators: false,
  experimental: {
    // Because src/proxy.ts exists, Next.js keeps a copy of every request body and cuts it off at
    // 10 MB by default; a cut-off upload then hangs in the /api rewrite until it times out. The
    // API accepts resumes up to 10 MB plus the multipart framing, so the copy must be allowed to
    // be larger than that. Anything bigger is still refused by the API itself (413).
    proxyClientMaxBodySize: "12mb",
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
