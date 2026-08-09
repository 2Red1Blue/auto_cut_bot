import { create } from "zustand";

interface SettingsStore {
  theme: "light" | "dark" | "system";
  language: string;
  backendUrl: string;
  setTheme: (theme: "light" | "dark" | "system") => void;
  setLanguage: (language: string) => void;
  setBackendUrl: (url: string) => void;
}

export const useSettingsStore = create<SettingsStore>((set) => ({
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
}));