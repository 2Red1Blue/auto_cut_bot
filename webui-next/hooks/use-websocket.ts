"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { wsClient } from "@/lib/ws-client";
import { useMessageStore } from "@/lib/stores/message-store";

export function useWebSocket(sessionId: string | null) {
  const addMessage = useMessageStore((s) => s.addMessage);
  const updateMessage = useMessageStore((s) => s.updateMessage);
  const [connected, setConnected] = useState(false);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    wsClient.connect();
    setConnected(true);

    return () => {
      wsClient.disconnect();
      setConnected(false);
    };
  }, []);

  useEffect(() => {
    if (!sessionId) return;

    // Clean up previous session handlers
    cleanupRef.current?.();

    // Subscribe to message events for this session
    const unsubs: (() => void)[] = [];

    unsubs.push(
      wsClient.on("message.created", (data: any) => {
        if (data.sessionId === sessionId) {
          addMessage(sessionId, data.message);
        }
      })
    );

    unsubs.push(
      wsClient.on("message.updated", (data: any) => {
        if (data.sessionId === sessionId) {
          updateMessage(sessionId, data.messageId, data.message);
        }
      })
    );

    unsubs.push(
      wsClient.on("message.streaming", (data: any) => {
        if (data.sessionId === sessionId) {
          updateMessage(sessionId, data.messageId, {
            content: data.content,
            streaming: true,
          });
        }
      })
    );

    cleanupRef.current = () => {
      unsubs.forEach((fn) => fn());
    };

    return () => {
      cleanupRef.current?.();
    };
  }, [sessionId, addMessage, updateMessage]);

  const send = useCallback(
    (data: unknown) => {
      wsClient.send(data);
    },
    []
  );

  return { connected, send };
}