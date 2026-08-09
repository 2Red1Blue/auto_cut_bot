"use client";

import { useEffect, useCallback } from "react";
import { useSessionStore } from "@/lib/stores/session-store";
import { apiClient } from "@/lib/api-client";

export function useSessions() {
  const sessions = useSessionStore((s) => s.sessions);
  const isLoading = useSessionStore((s) => s.isLoading);
  const fetchSessions = useSessionStore((s) => s.fetchSessions);
  const createSession = useSessionStore((s) => s.createSession);
  const deleteSession = useSessionStore((s) => s.deleteSession);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  const refresh = useCallback(() => {
    fetchSessions();
  }, [fetchSessions]);

  return {
    sessions,
    isLoading,
    refresh,
    createSession,
    deleteSession,
  };
}

// Direct API hooks for components that don't need Zustand
export function useSessionList() {
  return useSessions();
}