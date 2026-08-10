import { create } from "zustand";
import { apiClient } from "@/lib/api-client";
import type { ChatSummary } from "@/lib/types";

export interface Session {
  id: string;       // mapped from API's "key"
  title?: string;
  preview?: string;
  createdAt?: string;
  updatedAt?: string;
}

interface SessionStore {
  sessions: Session[];
  currentSessionId: string | null;
  isLoading: boolean;
  setSessions: (sessions: Session[]) => void;
  setCurrentSession: (id: string | null) => void;
  deleteSession: (id: string) => Promise<void>;
  fetchSessions: () => Promise<void>;
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  sessions: [],
  currentSessionId: null,
  isLoading: false,

  setSessions: (sessions) => set({ sessions }),

  setCurrentSession: (id) => {
    set({ currentSessionId: id });
  },

  deleteSession: async (id) => {
    try {
      await apiClient.deleteSession(id);
    } catch {
      // ignore — API may not support DELETE
    }
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== id),
      currentSessionId: state.currentSessionId === id ? null : state.currentSessionId,
    }));
  },

  fetchSessions: async () => {
    set({ isLoading: true });
    try {
      const data = await apiClient.getSessions();
      // API returns { sessions: [...] } or an array directly
      const raw = Array.isArray(data)
        ? data
        : (data as Record<string, unknown>).sessions
          ? ((data as Record<string, unknown>).sessions as ChatSummary[])
          : [];
      // Map API response (key-based) to Session (id-based)
      const sessions: Session[] = (raw as unknown as Array<Record<string, unknown>>).map((s) => ({
        id: (s.key as string) || "",
        title: (s.title as string) || undefined,
        preview: (s.preview as string) || undefined,
        createdAt: (s.createdAt ?? s.created_at) as string || undefined,
        updatedAt: (s.updatedAt ?? s.updated_at) as string || undefined,
      }));
      set({ sessions });
    } catch (err) {
      console.error("Failed to fetch sessions:", err);
    } finally {
      set({ isLoading: false });
    }
  },
}));
