import { create } from "zustand";

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  updatedAt?: string;
  streaming?: boolean;
}

interface MessageStore {
  messagesBySession: Record<string, Message[]>;
  isLoading: boolean;
  addMessage: (sessionId: string, message: Message) => void;
  updateMessage: (sessionId: string, messageId: string, updates: Partial<Message>) => void;
  sendMessage: (sessionId: string, content: string) => Promise<void>;
  fetchMessages: (sessionId: string) => Promise<void>;
}

export const useMessageStore = create<MessageStore>((set, get) => ({
  messagesBySession: {},
  isLoading: false,

  addMessage: (sessionId, message) => {
    set((state) => ({
      messagesBySession: {
        ...state.messagesBySession,
        [sessionId]: [
          ...(state.messagesBySession[sessionId] ?? []),
          message,
        ],
      },
    }));
  },

  updateMessage: (sessionId, messageId, updates) => {
    set((state) => ({
      messagesBySession: {
        ...state.messagesBySession,
        [sessionId]: (state.messagesBySession[sessionId] ?? []).map((m) =>
          m.id === messageId ? { ...m, ...updates } : m
        ),
      },
    }));
  },

  sendMessage: async (sessionId, content) => {
    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: "user",
      content,
      createdAt: new Date().toISOString(),
    };

    const assistantMsg: Message = {
      id: `assistant-${Date.now()}`,
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString(),
      streaming: true,
    };

    get().addMessage(sessionId, userMsg);
    get().addMessage(sessionId, assistantMsg);

    try {
      const res = await fetch("/api/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, content }),
      });

      if (!res.ok) {
        throw new Error("Failed to send message");
      }

      const data = await res.json();
      get().updateMessage(sessionId, assistantMsg.id, {
        content: data.content ?? data.message ?? JSON.stringify(data),
        streaming: false,
      });
    } catch (err) {
      console.error("Failed to send message:", err);
      get().updateMessage(sessionId, assistantMsg.id, {
        content: "Error: Failed to send message. Please try again.",
        streaming: false,
      });
    }
  },

  fetchMessages: async (sessionId) => {
    set({ isLoading: true });
    try {
      const res = await fetch(`/api/sessions/${sessionId}`);
      if (!res.ok) throw new Error("Failed to fetch messages");
      const data = await res.json();
      const messages = data.messages ?? data;
      set((state) => ({
        messagesBySession: {
          ...state.messagesBySession,
          [sessionId]: Array.isArray(messages) ? messages : [],
        },
      }));
    } catch (err) {
      console.error("Failed to fetch messages:", err);
    } finally {
      set({ isLoading: false });
    }
  },
}));