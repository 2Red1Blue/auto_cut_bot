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
  renameSession: (id: string, title: string) => Promise<void>;
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
    // Find all sessions with this UUID (might have different prefix levels)
    const sessionsToDelete = get().sessions.filter(s => {
      // Extract UUID from session.id (remove all websocket: prefixes)
      let sessionId = s.id;
      while (sessionId.startsWith("websocket:")) {
        sessionId = sessionId.slice(10);
      }
      let targetId = id;
      while (targetId.startsWith("websocket:")) {
        targetId = targetId.slice(10);
      }
      return sessionId === targetId;
    });
    
    // Delete all matching sessions
    for (const session of sessionsToDelete) {
      try {
        await apiClient.deleteSession(session.id);
      } catch (err) {
        console.error(`Failed to delete session ${session.id}:`, err);
      }
    }
    
    // Remove all deleted sessions from store
    const deletedIds = new Set(sessionsToDelete.map(s => s.id));
    set((state) => ({
      sessions: state.sessions.filter((s) => !deletedIds.has(s.id)),
      currentSessionId: state.currentSessionId && deletedIds.has(state.currentSessionId) 
        ? null 
        : state.currentSessionId,
    }));
  },

  renameSession: async (id, title) => {
    // Optimistically update local state
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, title } : s
      ),
    }));
    try {
      await apiClient.renameSession(id, title);
    } catch {
      // ignore — API may not support PATCH
    }
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
      const sessions: Session[] = (raw as unknown as Array<Record<string, unknown>>).map((s) => {
        const key = (s.key as string) || "";
        // Strip channel prefix (e.g. "websocket:uuid" -> "uuid")
        // Handle multi-level prefixes like "websocket:websocket:uuid"
        let id = key;
        while (id.startsWith("websocket:")) {
          id = id.slice("websocket:".length);
        }
        return {
          id,
          title: (s.title as string) || undefined,
          preview: (s.preview as string) || undefined,
          createdAt: (s.createdAt ?? s.created_at) as string || undefined,
          updatedAt: (s.updatedAt ?? s.updated_at) as string || undefined,
        };
      });
      // Deduplicate by id (keep latest updated)
      const seen = new Map<string, Session>();
      for (const s of sessions) {
        const existing = seen.get(s.id);
        if (!existing || (s.updatedAt && (!existing.updatedAt || s.updatedAt > existing.updatedAt))) {
          seen.set(s.id, s);
        }
      }
      set({ sessions: Array.from(seen.values()) });
    } catch (err) {
      console.error("Failed to fetch sessions:", err);
    } finally {
      set({ isLoading: false });
    }
  },
}));
