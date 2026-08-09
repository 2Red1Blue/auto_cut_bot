// Session types
export interface Session {
  id: string;
  title?: string;
  createdAt?: string;
  updatedAt?: string;
}

// Message types
export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  updatedAt?: string;
  streaming?: boolean;
}

// Config types
export interface BackendConfig {
  channels: Record<string, unknown>[];
  tools: Record<string, unknown>[];
  providers: Record<string, unknown>[];
  mcp: Record<string, unknown>[];
  skills: Record<string, unknown>[];
}

// Theme types
export type Theme = "light" | "dark" | "system";

// Language types
export type SupportedLocale =
  | "en"
  | "zh-CN"
  | "zh-TW"
  | "ja"
  | "ko"
  | "fr"
  | "es"
  | "pt-BR"
  | "vi"
  | "id";