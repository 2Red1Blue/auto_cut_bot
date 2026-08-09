"use client";

import { createContext, useContext, useRef } from "react";

interface WebSocketContextValue {
  connect: (url: string) => WebSocket;
}

const WebSocketContext = createContext<WebSocketContextValue>({
  connect: () => {
    throw new Error("WebSocketProvider not mounted");
  },
});

export function useWebSocketContext() {
  return useContext(WebSocketContext);
}

export function WebSocketProvider({ children }: { children: React.ReactNode }) {
  const connectionsRef = useRef<Map<string, WebSocket>>(new Map());

  const connect = (url: string): WebSocket => {
    const existing = connectionsRef.current.get(url);
    if (existing && existing.readyState === WebSocket.OPEN) {
      return existing;
    }

    const ws = new WebSocket(url);
    connectionsRef.current.set(url, ws);

    ws.onclose = () => {
      connectionsRef.current.delete(url);
    };

    return ws;
  };

  return (
    <WebSocketContext.Provider value={{ connect }}>
      {children}
    </WebSocketContext.Provider>
  );
}