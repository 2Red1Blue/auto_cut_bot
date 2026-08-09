import type { NextConfig } from "next";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8765";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      // API proxy — route backend requests through Next.js
      { source: "/api/sessions", destination: `${BACKEND_URL}/api/sessions` },
      { source: "/api/sessions/:id", destination: `${BACKEND_URL}/api/sessions/:id` },
      { source: "/api/messages", destination: `${BACKEND_URL}/api/messages` },
      { source: "/api/config", destination: `${BACKEND_URL}/api/config` },
      { source: "/api/channels", destination: `${BACKEND_URL}/api/channels` },
      { source: "/api/tools", destination: `${BACKEND_URL}/api/tools` },
      { source: "/api/providers", destination: `${BACKEND_URL}/api/providers` },
      { source: "/api/mcp", destination: `${BACKEND_URL}/api/mcp` },
      { source: "/api/skills", destination: `${BACKEND_URL}/api/skills` },
      { source: "/api/auth", destination: `${BACKEND_URL}/api/auth` },
      // WebUI static files (channel webui)
      { source: "/webui/:path*", destination: `${BACKEND_URL}/webui/:path*` },
      // Auth endpoints
      { source: "/auth/:path*", destination: `${BACKEND_URL}/auth/:path*` },
    ];
  },
};

export default nextConfig;