import type { NextConfig } from "next";

// The browser only ever talks to this Next.js server (same origin), which forwards /api/* to
// the FastAPI backend. That keeps CORS out of the backend entirely.
const API_ORIGIN = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const isDev = process.env.NODE_ENV === "development";

// The Content-Security-Policy. The page loads only its own scripts, styles and one SVG icon, and
// calls only its own origin (the /api/* proxy), so everything is 'self'.
//
// * script-src needs 'unsafe-inline' because Next.js writes small inline scripts (the hydration
//   data) into every page. A nonce-based policy would remove that, but it needs middleware and
//   dynamic rendering of every page; this app is two static pages, so 'unsafe-inline' is the
//   documented trade-off. It still blocks every foreign script origin, plugins, framing, base-tag
//   and form hijacking.
// * style-src 'unsafe-inline' is needed by Next.js (it inlines critical CSS and the dev overlay).
// * img-src allows data: for inlined images; the only image is /icon.svg.
// * Development only: 'unsafe-eval' (React Refresh and source maps use eval) and ws: (the
//   hot-reload websocket). Neither is in the production policy.
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self'${isDev ? " ws: wss:" : ""}`,
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

// No Strict-Transport-Security on purpose: the app is plain HTTP on localhost, and the header
// belongs on the layer that terminates TLS.
const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

const config: NextConfig = {
  poweredByHeader: false, // no "X-Powered-By: Next.js"
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
