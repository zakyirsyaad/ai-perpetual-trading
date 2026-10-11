import type { NextConfig } from "next";
import path from "node:path";

// When set, the server forwards /feed/* to the bot's loopback telemetry listener, so one
// public hostname serves both the screen and its data. Read at `next build` time. Leave it
// unset on a laptop, where the feed is reached through an SSH tunnel instead.
const feedProxyTarget = process.env.FEED_PROXY_TARGET?.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  // The repo root is a Python project; pin the workspace root to this app.
  turbopack: { root: path.resolve(__dirname) },
  // Agent rules live in the repo-root CLAUDE.md. Do not emit AGENTS.md here.
  agentRules: false,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // gzip buffers a Server-Sent Events stream until it fills, which freezes the live feed.
  compress: false,
  async rewrites() {
    if (!feedProxyTarget) return [];
    return [{ source: "/feed/:path*", destination: `${feedProxyTarget}/:path*` }];
  },
};

export default nextConfig;
