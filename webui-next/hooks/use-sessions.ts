"use client";

import { useEffect, useCallback, useRef } from "react";
import { useClient } from "@/providers/client-provider";
import { useSessionStore } from "@/lib/stores/session-store";
import type { ChatSummary, WorkspaceScopePayload } from "@/lib/types";

/**
 * Bridges NanobotClient session events to Zustand session-store.
 * Handles session list, create, delete, and real-time updates.
 */
export function useSessions() {
  const { client, status } = useClient();
  const sessions = useSessionStore((s) => s.sessions);
  const isLoading = useSessionStore((s) => s.isLoading);
  const fetchSessions = useSessionStore((s) => s.fetchSessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const setCurrentSession = useSessionStore((s) => s.setCurrentSession);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const syncedRef = useRef(false);

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
          // Refresh session list when metadata changes
          fetchSessions();
        }
      }
    );

    return () => unsub();
  }, [client, status, fetchSessions]);

  // Create a new chat session via NanobotClient
  const createSession = useCallback(async () => {
    if (!client) return null;

    try {
      // Use the REST API to create a session
      const session = await useSessionStore.getState().createSession();
      // Attach the NanobotClient to this chat
      client.attach(session.id);
      return session;
    } catch (err) {
      console.error("Failed to create session:", err);
      return null;
    }
  }, [client]);

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