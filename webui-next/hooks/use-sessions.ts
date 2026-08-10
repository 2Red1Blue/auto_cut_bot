"use client";

import { useEffect, useCallback, useRef } from "react";
import { useClient } from "@/providers/client-provider";
import { useSessionStore } from "@/lib/stores/session-store";
import type { WorkspaceScopePayload } from "@/lib/types";

/**
 * Bridges NanobotClient session events to Zustand session-store.
 * Handles session list, create, delete, and real-time updates.
 */
export function useSessions() {
  const { client, status } = useClient();
  const sessions = useSessionStore((s) => s.sessions);
  const isLoading = useSessionStore((s) => s.isLoading);
  const fetchSessions = useSessionStore((s) => s.fetchSessions);
  const setCurrentSession = useSessionStore((s) => s.setCurrentSession);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);

  // Fetch sessions from REST API on mount
  useEffect(() => {
    if (status === "ready") {
      fetchSessions();
    }
  }, [status, fetchSessions]);

  // Subscribe to real-time session updates via NanobotClient
  useEffect(() => {
    if (!client || status !== "ready") return;

    const unsub = client.onSessionUpdate(
      (chatId: string, scope?: string, workspaceScope?: WorkspaceScopePayload) => {
        if (scope === "metadata") {
          fetchSessions();
        }
      }
    );

    return () => unsub();
  }, [client, status, fetchSessions]);

  // Create a new chat session via WebSocket new_chat message
  const createSession = useCallback(async () => {
    if (!client) return null;

    try {
      const chatId = await client.newChat();
      // Refresh session list from backend
      await fetchSessions();
      // Select the new session
      setCurrentSession(chatId);
      return { id: chatId };
    } catch (err) {
      console.error("Failed to create session:", err);
      return null;
    }
  }, [client, fetchSessions, setCurrentSession]);

  // Delete a session
  const deleteSession = useCallback(
    async (id: string) => {
      await useSessionStore.getState().deleteSession(id);
    },
    []
  );

  return {
    sessions,
    isLoading,
    currentSessionId,
    setCurrentSession,
    createSession,
    deleteSession,
    refresh: fetchSessions,
  };
}
