import { create } from "zustand";
import { apiClient } from "@/lib/api-client";

export interface Session {
  id: string;
  title?: string;
  createdAt?: string;
  updatedAt?: string;
}

interface SessionStore {
  sessions: Session[];
  currentSessionId: string | null;
  isLoading: boolean;
  setSessions: (sessions: Session[]) => void;
  setCurrentSession: (id: string | null) => void;
  createSession: () => Promise<Session>;
  deleteSession: (id: string) => Promise<void>;
  fetchSessions: () => Promise<void>;
}

export const useSessionStore = create<SessionStore>((set) => ({
  sessions: [],
  currentSessionId: null,
  isLoading: false,

  setSessions: (sessions) => set({ sessions }),

  setCurrentSession: (id) => {
    set({ currentSessionId: id });
    if (id) {
      window.history.pushState(null, "", `/${id}`);
    } else {
      window.history.pushState(null, "", "/");
    }
  },

  createSession: async () => {
    const session = await apiClient.createSession();
    set((state) => ({
      sessions: [session, ...state.sessions],
      currentSessionId: session.id,
    }));
    return session;
  },

  deleteSession: async (id) => {
    await apiClient.deleteSession(id);
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== id),
      currentSessionId: state.currentSessionId === id ? null : state.currentSessionId,
    }));
  },

  fetchSessions: async () => {
    set({ isLoading: true });
    try {
      const sessions = await apiClient.getSessions();
      set({ sessions: Array.isArray(sessions) ? sessions : [] });
    } catch (err) {
      console.error("Failed to fetch sessions:", err);
    } finally {
      set({ isLoading: false });
    }
  },
}));