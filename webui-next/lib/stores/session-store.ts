import { create } from "zustand";

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

export const useSessionStore = create<SessionStore>((set, get) => ({
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
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) throw new Error("Failed to create session");
    const session = await res.json();
    set((state) => ({
      sessions: [session, ...state.sessions],
      currentSessionId: session.id,
    }));
    return session;
  },

  deleteSession: async (id) => {
    await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== id),
      currentSessionId:
        state.currentSessionId === id ? null : state.currentSessionId,
    }));
  },

  fetchSessions: async () => {
    set({ isLoading: true });
    try {
      const res = await fetch("/api/sessions");
      if (!res.ok) throw new Error("Failed to fetch sessions");
      const sessions = await res.json();
      set({ sessions: Array.isArray(sessions) ? sessions : [] });
    } catch (err) {
      console.error("Failed to fetch sessions:", err);
    } finally {
      set({ isLoading: false });
    }
  },
}));