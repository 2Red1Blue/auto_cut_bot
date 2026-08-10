import { create } from "zustand";

const SIDEBAR_STORAGE_KEY = "auto_cut_bot-webui.sidebar-open";

function loadSidebarPref(): boolean {
  try {
    const raw = localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (raw === "0" || raw === "false") return false;
    return true;
  } catch {
    return true;
  }
}

function saveSidebarPref(open: boolean): void {
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, open ? "1" : "0");
  } catch {
    // ignore storage errors
  }
}

interface UIStore {
  sidebarOpen: boolean;
  settingsOpen: boolean;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setSettingsOpen: (open: boolean) => void;
}

export const useUIStore = create<UIStore>((set) => ({
  sidebarOpen: typeof window !== "undefined" ? loadSidebarPref() : true,
  settingsOpen: false,

  toggleSidebar: () =>
    set((s) => {
      const next = !s.sidebarOpen;
      saveSidebarPref(next);
      return { sidebarOpen: next };
    }),
  setSidebarOpen: (open) => {
    saveSidebarPref(open);
    set({ sidebarOpen: open });
  },
  setSettingsOpen: (open) => set({ settingsOpen: open }),
}));