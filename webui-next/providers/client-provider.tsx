"use client";

import { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from "react";
import { NanobotClient } from "@/lib/auto_cut_bot-client";
import { apiClient } from "@/lib/api-client";
import {
  fetchBootstrap,
  loadSavedSecret,
  saveSecret,
  clearSavedSecret,
  consumeUrlBootstrapSecret,
  deriveWsUrl,
  BootstrapAuthRequiredError,
} from "@/lib/bootstrap";
import { createRuntimeHost, initializeLoopbackRuntimeHost } from "@/lib/runtime";
import type { BootstrapResponse, ConnectionStatus } from "@/lib/types";

interface ClientContextValue {
  client: NanobotClient | null;
  status: "loading" | "auth" | "connecting" | "ready" | "error";
  error: string | null;
  token: string | null;
  modelName: string | null;
  bootstrap: BootstrapResponse | null;
  login: (secret: string) => Promise<void>;
  logout: () => void;
  connectionStatus: ConnectionStatus;
}

const ClientContext = createContext<ClientContextValue>({
  client: null,
  status: "loading",
  error: null,
  token: null,
  modelName: null,
  bootstrap: null,
  login: async () => {},
  logout: () => {},
  connectionStatus: "idle",
});

export function useClient() {
  return useContext(ClientContext);
}

export function ClientProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ClientContextValue["status"]>("loading");
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [modelName, setModelName] = useState<string | null>(null);
  const [bootstrap, setBootstrap] = useState<BootstrapResponse | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("idle");
  const clientRef = useRef<NanobotClient | null>(null);

  // Initialize loopback host bridge
  useEffect(() => {
    initializeLoopbackRuntimeHost();
  }, []);

  // Auto-bootstrap on mount
  useEffect(() => {
    const savedSecret = loadSavedSecret();
    const urlSecret = consumeUrlBootstrapSecret();

    const secret = urlSecret || savedSecret;
    if (secret) {
      if (urlSecret) saveSecret(urlSecret);
      doBootstrap(secret);
    } else {
      setStatus("auth");
    }
  }, []);

  const doBootstrap = useCallback(async (secret: string) => {
    setStatus("connecting");
    setError(null);

    try {
      const result = await fetchBootstrap("", secret);
      setBootstrap(result);
      setToken(result.token ?? null);
      setModelName(result.model_name ?? null);
      
      // Set api_token for HTTP API client (token is for WebSocket)
      apiClient.setToken(result.api_token ?? result.token ?? null);

      const runtimeHost = createRuntimeHost(
        result.runtime_surface ?? "browser",
        result.runtime_capabilities ?? null
      );

      const wsUrl = deriveWsUrl(result.ws_path, result.token, result.ws_url);

      const client = new NanobotClient({
        url: wsUrl,
        socketFactory: runtimeHost.socketFactory,
      });

      client.onStatus((s) => setConnectionStatus(s));
      client.onRuntimeModelUpdate((m) => setModelName(m));
      client.connect();

      clientRef.current = client;
      setStatus("ready");
    } catch (err) {
      if (err instanceof BootstrapAuthRequiredError) {
        setStatus("auth");
        setError("Authentication failed. Please check your secret.");
      } else {
        setStatus("error");
        setError(err instanceof Error ? err.message : "Connection failed");
      }
    }
  }, []);

  const login = useCallback(async (secret: string) => {
    saveSecret(secret);
    await doBootstrap(secret);
  }, [doBootstrap]);

  const logout = useCallback(() => {
    clearSavedSecret();
    clientRef.current?.close();
    clientRef.current = null;
    setToken(null);
    setModelName(null);
    setBootstrap(null);
    setStatus("auth");
    setConnectionStatus("idle");
  }, []);

  return (
    <ClientContext.Provider
      value={{
        client: clientRef.current,
        status,
        error,
        token,
        modelName,
        bootstrap,
        login,
        logout,
        connectionStatus,
      }}
    >
      {children}
    </ClientContext.Provider>
  );
}