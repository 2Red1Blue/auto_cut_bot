"use client";

import { useEffect } from "react";
import { useParams } from "next/navigation";
import { ChatContainer } from "@/components/chat/chat-container";
import { useSessionStore } from "@/lib/stores/session-store";

export default function SessionPage() {
  const params = useParams();
  const sessionId = params.sessionId as string;
  const setCurrentSession = useSessionStore((s) => s.setCurrentSession);

  useEffect(() => {
    if (sessionId) {
      setCurrentSession(sessionId);
    }
  }, [sessionId, setCurrentSession]);

  return <ChatContainer />;
}
