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

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  cacheComponents: true,
  partialPrefetching: true,
  // The dev server is opened as http://127.0.0.1:<port> as well as http://localhost:<port>.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // The development badge would sit on top of the sidebar and the mobile layout.
  devIndicators: false,
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
