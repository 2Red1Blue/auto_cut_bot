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

  // Pipeline
  async getPipelineJobs() {
    return this.request<PipelineJob[]>("/api/pipeline/jobs");
  }

  async getPipelineJob(id: string) {
    return this.request<PipelineJob>(`/api/pipeline/jobs/${id}`);
  }

  async triggerPipeline(request: PipelineTriggerRequest) {
    return this.request<PipelineTriggerResponse>("/api/pipeline/run", {
      method: "POST",
      body: request,
    });
  }

  async cancelPipelineJob(id: string) {
    return this.request<void>(`/api/pipeline/jobs/${id}/cancel`, { method: "POST" });
  }

  // Media Library
  async getMediaLibrary(folderId?: string) {
    const params = folderId ? `?folderId=${folderId}` : "";
    return this.request<MediaLibrary>(`/api/media${params}`);
  }

  async getMediaAsset(id: string) {
    return this.request<MediaAsset>(`/api/media/assets/${id}`);
  }

  async deleteMediaAsset(id: string) {
    return this.request<void>(`/api/media/assets/${id}`, { method: "DELETE" });
  }

  async createMediaFolder(name: string, parentId?: string) {
    return this.request<MediaFolder>("/api/media/folders", {
      method: "POST",
      body: { name, parentId },
    });
  }

  async uploadMediaAsset(file: File, folderId?: string, onProgress?: (progress: number) => void) {
    const formData = new FormData();
    formData.append("file", file);
    if (folderId) {
      formData.append("folderId", folderId);
    }

    return new Promise<MediaAsset>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(event.loaded / event.total);
        }
      });

      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          reject(new Error(`Upload failed: ${xhr.statusText}`));
        }
      });

      xhr.addEventListener("error", () => {
        reject(new Error("Upload failed"));
      });

      xhr.open("POST", `${this.baseUrl}/api/media/upload`);
      xhr.send(formData);
    });
  }
}

export const apiClient = new ApiClient();

// Re-export types for convenience
export type { PipelineJob, PipelineTriggerRequest, PipelineTriggerResponse, MediaAsset, MediaFolder, MediaLibrary } from "./types/pipeline";

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