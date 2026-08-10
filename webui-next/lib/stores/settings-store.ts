import { create } from "zustand";

/** Pipeline-related settings mirroring PipelineConfig fields */
interface PipelineSettings {
  windowSeconds: number;
  overlapSeconds: number;
  workers: number | string; // "auto" | number
  rpmLimit: number;
  backend: string; // "qwen" | "doubao" | etc.
}

interface SettingsStore {
  theme: "light" | "dark" | "system";
  language: string;
  backendUrl: string;
  /** General / LLM */
  model: string;
  provider: string;
  temperature: number;
  maxTokens: number;
  botName: string;
  /** Pipeline */
  pipeline: PipelineSettings;
  /** Actions */
  setTheme: (theme: "light" | "dark" | "system") => void;
  setLanguage: (language: string) => void;
  setBackendUrl: (url: string) => void;
  setModel: (model: string) => void;
  setProvider: (provider: string) => void;
  setTemperature: (temperature: number) => void;
  setMaxTokens: (maxTokens: number) => void;
  setBotName: (botName: string) => void;
  setPipeline: (pipeline: Partial<PipelineSettings>) => void;
}

const DEFAULT_PIPELINE: PipelineSettings = {
  windowSeconds: 240,
  overlapSeconds: 12,
  workers: "auto",
  rpmLimit: 0,
  backend: "qwen",
};

function loadFromStorage<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function saveToStorage(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore
  }
}

export const useSettingsStore = create<SettingsStore>((set, get) => ({
  theme: (typeof window !== "undefined"
    ? (localStorage.getItem("theme") as "light" | "dark" | "system") ?? "system"
    : "system"),
  language:
    typeof window !== "undefined"
      ? localStorage.getItem("language") ?? "zh-CN"
      : "zh-CN",
  backendUrl:
    typeof window !== "undefined"
      ? localStorage.getItem("backendUrl") ?? "http://localhost:8765"
      : "http://localhost:8765",

  model: loadFromStorage<string>("auto_cut_bot.model", "qwen3-max"),
  provider: loadFromStorage<string>("auto_cut_bot.provider", "openai"),
  temperature: loadFromStorage<number>("auto_cut_bot.temperature", 0.7),
  maxTokens: loadFromStorage<number>("auto_cut_bot.maxTokens", 4096),
  botName: loadFromStorage<string>("auto_cut_bot.botName", "AutoCutBot"),

  pipeline: loadFromStorage<PipelineSettings>("auto_cut_bot.pipeline", DEFAULT_PIPELINE),

  setTheme: (theme) => {
    localStorage.setItem("theme", theme);
    set({ theme });
  },

  setLanguage: (language) => {
    localStorage.setItem("language", language);
    set({ language });
  },

  setBackendUrl: (url) => {
    localStorage.setItem("backendUrl", url);
    set({ backendUrl: url });
  },

  setModel: (model) => {
    saveToStorage("auto_cut_bot.model", model);
    set({ model });
  },

  setProvider: (provider) => {
    saveToStorage("auto_cut_bot.provider", provider);
    set({ provider });
  },

  setTemperature: (temperature) => {
    saveToStorage("auto_cut_bot.temperature", temperature);
    set({ temperature });
  },

  setMaxTokens: (maxTokens) => {
    saveToStorage("auto_cut_bot.maxTokens", maxTokens);
    set({ maxTokens });
  },

  setBotName: (botName) => {
    saveToStorage("auto_cut_bot.botName", botName);
    set({ botName });
  },

  setPipeline: (partial) => {
    const next = { ...get().pipeline, ...partial };
    saveToStorage("auto_cut_bot.pipeline", next);
    set({ pipeline: next });
  },
}));