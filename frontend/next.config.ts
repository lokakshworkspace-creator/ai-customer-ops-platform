import type { NextConfig } from "next";

// The browser only ever talks to this Next.js server (same origin), which forwards /api/* to
// the FastAPI backend. That keeps CORS out of the backend entirely.
const API_ORIGIN = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
