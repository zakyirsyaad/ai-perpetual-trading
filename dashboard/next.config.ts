import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // The repo root is a Python project; pin the workspace root to this app.
  turbopack: { root: path.resolve(__dirname) },
  // Agent rules live in the repo-root CLAUDE.md. Do not emit AGENTS.md here.
  agentRules: false,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default nextConfig;
