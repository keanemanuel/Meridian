import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */

  // On Vercel, vercel.json routes `/api/*` straight to the Python function —
  // this never runs there. Locally there is no such router in front of
  // `next dev`, so proxy the same relative path to the FastAPI dev server
  // (`uvicorn ... --port 8000`) instead. Keeps NEXT_PUBLIC_API_URL="/api"
  // correct in both places.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
