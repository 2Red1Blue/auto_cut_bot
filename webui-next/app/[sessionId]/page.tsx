"use client";

import { useEffect } from "react";
import { useParams } from "next/navigation";
import { ChatContainer } from "@/components/chat/chat-container";
import { useSessionStore } from "@/lib/stores/session-store";

/** Strip channel prefix from the session key (e.g. "websocket:uuid" -> "uuid"). */
function normalizeSessionId(raw: string): string {
  let id = raw;
  while (id.startsWith("websocket:")) {
    id = id.slice("websocket:".length);
  }
  return id;
}

export default function SessionPage() {
  const params = useParams();
  const sessionId = normalizeSessionId(params.sessionId as string);
  const setCurrentSession = useSessionStore((s) => s.setCurrentSession);

  useEffect(() => {
    if (sessionId) {
      setCurrentSession(sessionId);
    }
  }, [sessionId, setCurrentSession]);

  return <ChatContainer />;
}
