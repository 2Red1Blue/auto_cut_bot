import type { NextConfig } from "next";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8765";
const VIZ_SERVER_URL = process.env.VIZ_SERVER_URL || "http://localhost:8787";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      // Auto Cut Bot API proxy
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
      // Pipeline API
      { source: "/api/pipeline/:path*", destination: `${BACKEND_URL}/v1/pipeline/:path*` },
      // Media API (viz-server)
      { source: "/api/media/:path*", destination: `${VIZ_SERVER_URL}/api/media/:path*` },
      // WebUI static files (channel webui)
      { source: "/webui/:path*", destination: `${BACKEND_URL}/webui/:path*` },
      // Auth endpoints
      { source: "/auth/:path*", destination: `${BACKEND_URL}/auth/:path*` },
    ];
  },
};

export default nextConfig;