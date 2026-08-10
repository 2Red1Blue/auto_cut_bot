"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { wsClient } from "@/lib/ws-client";
import { useMessageStore } from "@/lib/stores/message-store";

// ---------------------------------------------------------------------------
// Reconnection configuration
// ---------------------------------------------------------------------------

const INITIAL_DELAY_MS = 1000;
const MAX_DELAY_MS = 30_000;
const BACKOFF_MULTIPLIER = 2;
const MAX_RETRIES = 10;

export type ConnectionStatus = "connected" | "disconnected" | "reconnecting";

export interface UseWebSocketOptions {
  /** Called after a successful reconnection. */
  onReconnect?: () => void;
  /** Called when the connection is lost and reconnection begins. */
  onDisconnect?: () => void;
  /** Called when max retries are exhausted. */
  onMaxRetriesExceeded?: () => void;
}

export interface UseWebSocketReturn {
  /** Whether the WebSocket is currently connected. */
  connected: boolean;
  /** Current connection status. */
  connectionStatus: ConnectionStatus;
  /** Number of reconnection attempts since the last successful connection. */
  retryCount: number;
  /** Send a message over the WebSocket. */
  send: (data: unknown) => void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useWebSocket(
  sessionId: string | null,
  options: UseWebSocketOptions = {}
): UseWebSocketReturn {
  const { onReconnect, onDisconnect, onMaxRetriesExceeded } = options;

  const addMessage = useMessageStore((s) => s.addMessage);
  const updateMessage = useMessageStore((s) => s.updateMessage);
  const [connected, setConnected] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("disconnected");
  const [retryCount, setRetryCount] = useState(0);

  const cleanupRef = useRef<(() => void) | null>(null);
  const retryCountRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldReconnectRef = useRef(true);
  const onReconnectRef = useRef(onReconnect);
  const onDisconnectRef = useRef(onDisconnect);
  const onMaxRetriesExceededRef = useRef(onMaxRetriesExceeded);

  // Keep callbacks in sync without re-triggering effects
  onReconnectRef.current = onReconnect;
  onDisconnectRef.current = onDisconnect;
  onMaxRetriesExceededRef.current = onMaxRetriesExceeded;

  // -----------------------------------------------------------------------
  // Exponential backoff reconnection
  // -----------------------------------------------------------------------

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const scheduleReconnect = useCallback(() => {
    clearReconnectTimer();

    if (retryCountRef.current >= MAX_RETRIES) {
      setConnectionStatus("disconnected");
      setConnected(false);
      onMaxRetriesExceededRef.current?.();
      return;
    }

    // Calculate delay with exponential backoff and jitter
    const delay = Math.min(
      INITIAL_DELAY_MS * Math.pow(BACKOFF_MULTIPLIER, retryCountRef.current),
      MAX_DELAY_MS
    );
    // Add up to 20% jitter to avoid thundering herd
    const jitter = delay * 0.2 * Math.random();
    const finalDelay = Math.round(delay + jitter);

    setConnectionStatus("reconnecting");
    setConnected(false);

    reconnectTimerRef.current = setTimeout(() => {
      retryCountRef.current += 1;
      setRetryCount(retryCountRef.current);
      wsClient.connect();
    }, finalDelay);
  }, [clearReconnectTimer]);

  // -----------------------------------------------------------------------
  // Initial connection
  // -----------------------------------------------------------------------

  useEffect(() => {
    shouldReconnectRef.current = true;
    retryCountRef.current = 0;
    setRetryCount(0);

    wsClient.connect();
    setConnected(true);
    setConnectionStatus("connected");

    return () => {
      shouldReconnectRef.current = false;
      clearReconnectTimer();
      wsClient.disconnect();
      setConnected(false);
      setConnectionStatus("disconnected");
    };
  }, [clearReconnectTimer]);

  // -----------------------------------------------------------------------
  // Listen for connection state changes
  // -----------------------------------------------------------------------

  useEffect(() => {
    const unsubOpen = wsClient.on("ws:open", () => {
      const wasReconnecting = retryCountRef.current > 0;
      retryCountRef.current = 0;
      setRetryCount(0);
      setConnected(true);
      setConnectionStatus("connected");

      if (wasReconnecting) {
        onReconnectRef.current?.();
      }
    });

    const unsubClose = wsClient.on("ws:close", () => {
      setConnected(false);
      onDisconnectRef.current?.();

      if (shouldReconnectRef.current) {
        scheduleReconnect();
      } else {
        setConnectionStatus("disconnected");
      }
    });

    return () => {
      unsubOpen();
      unsubClose();
    };
  }, [scheduleReconnect]);

  // -----------------------------------------------------------------------
  // Session-specific message handlers
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (!sessionId) return;

    // Clean up previous session handlers
    cleanupRef.current?.();

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

  return { connected, connectionStatus, retryCount, send };
}