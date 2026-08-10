import type { NextConfig } from "next";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8767";
const WEBSOCKET_URL = process.env.WEBSOCKET_URL || "http://localhost:8768";
const VIZ_SERVER_URL = process.env.VIZ_SERVER_URL || "http://localhost:8787";

const nextConfig: NextConfig = {
  output: "standalone",
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async rewrites() {
    return [
      // WebUI bootstrap endpoint (on WebSocket server, not HTTP API)
      { source: "/webui/bootstrap", destination: `${WEBSOCKET_URL}/webui/bootstrap` },
      // Other WebUI static files (on WebSocket server)
      { source: "/webui/:path*", destination: `${WEBSOCKET_URL}/webui/:path*` },
      // Auto Cut Bot API proxy (on WebSocket server)
      { source: "/api/sessions", destination: `${WEBSOCKET_URL}/api/sessions` },
      { source: "/api/messages", destination: `${WEBSOCKET_URL}/api/messages` },
      { source: "/api/config", destination: `${WEBSOCKET_URL}/api/config` },
      { source: "/api/channels", destination: `${WEBSOCKET_URL}/api/channels` },
      { source: "/api/tools", destination: `${WEBSOCKET_URL}/api/tools` },
      { source: "/api/providers", destination: `${WEBSOCKET_URL}/api/providers` },
      { source: "/api/mcp", destination: `${WEBSOCKET_URL}/api/mcp` },
      { source: "/api/skills", destination: `${WEBSOCKET_URL}/api/skills` },
      { source: "/api/auth", destination: `${WEBSOCKET_URL}/api/auth` },
      // Pipeline API (on backend)
      { source: "/api/pipeline/:path*", destination: `${BACKEND_URL}/v1/pipeline/:path*` },
      // Media API (viz-server)
      { source: "/api/media/:path*", destination: `${VIZ_SERVER_URL}/api/media/:path*` },
      // Auth endpoints (on backend)
      { source: "/auth/:path*", destination: `${BACKEND_URL}/auth/:path*` },
    ];
  },
};

export default nextConfig;