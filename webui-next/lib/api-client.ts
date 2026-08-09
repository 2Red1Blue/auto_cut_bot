const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8765";

interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
  signal?: AbortSignal;
}

class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = BACKEND_URL) {
    this.baseUrl = baseUrl;
  }

  private async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { method = "GET", headers = {}, body, signal } = options;

    const url = `${this.baseUrl}${path}`;
    const config: RequestInit = {
      method,
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
      signal,
    };

    if (body) {
      config.body = JSON.stringify(body);
    }

    const res = await fetch(url, config);

    if (!res.ok) {
      const error = await res.text().catch(() => "Unknown error");
      throw new Error(`API Error ${res.status}: ${error}`);
    }

    return res.json();
  }

  // Sessions
  async getSessions() {
    return this.request<Session[]>("/api/sessions");
  }

  async createSession() {
    return this.request<Session>("/api/sessions", { method: "POST" });
  }

  async getSession(id: string) {
    return this.request<Session>("/api/sessions/" + id);
  }

  async deleteSession(id: string) {
    return this.request<void>("/api/sessions/" + id, { method: "DELETE" });
  }

  // Messages
  async getMessages(sessionId: string) {
    return this.request<Message[]>("/api/sessions/" + sessionId + "/messages");
  }

  async sendMessage(sessionId: string, content: string) {
    return this.request<Message>("/api/messages", {
      method: "POST",
      body: { sessionId, content },
    });
  }

  // Config
  async getConfig() {
    return this.request<Record<string, unknown>>("/api/config");
  }

  // Channels
  async getChannels() {
    return this.request<Record<string, unknown>[]>("/api/channels");
  }

  // Tools
  async getTools() {
    return this.request<Record<string, unknown>[]>("/api/tools");
  }

  // Providers
  async getProviders() {
    return this.request<Record<string, unknown>[]>("/api/providers");
  }
}

export const apiClient = new ApiClient();

export interface Session {
  id: string;
  title?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  updatedAt?: string;
}