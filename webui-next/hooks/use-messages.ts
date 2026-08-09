"use client";

import { useEffect, useCallback } from "react";
import { useMessageStore } from "@/lib/stores/message-store";

export function useMessages(sessionId: string) {
  const messages = useMessageStore(
    (s) => s.messagesBySession[sessionId] ?? []
  );
  const isLoading = useMessageStore((s) => s.isLoading);
  const fetchMessages = useMessageStore((s) => s.fetchMessages);
  const sendMessage = useMessageStore((s) => s.sendMessage);

  useEffect(() => {
    if (sessionId) {
      fetchMessages(sessionId);
    }
  }, [sessionId, fetchMessages]);

  const refresh = useCallback(() => {
    if (sessionId) {
      fetchMessages(sessionId);
    }
  }, [sessionId, fetchMessages]);

  return {
    messages,
    isLoading,
    sendMessage: (content: string) => sendMessage(sessionId, content),
    refresh,
  };
}