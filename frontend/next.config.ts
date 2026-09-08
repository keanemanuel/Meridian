import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */

  // Local dev only: `next dev` has no router in front of it, so proxy the
  // relative `/api/*` path to the FastAPI dev server (`uvicorn ... --port
  // 8000`), keeping NEXT_PUBLIC_API_URL unset / "/api" workable on a laptop.
  //
  // In production the FastAPI backend runs on Railway (docs/DEPLOY.md), and
  // NEXT_PUBLIC_API_URL is set to that absolute URL — every call in
  // lib/api.ts then goes straight there, so this rewrite is never consulted.
  // Guarded on NODE_ENV so the Vercel build carries no dangling proxy rule.
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
