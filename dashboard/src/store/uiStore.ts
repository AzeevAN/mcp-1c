import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Theme = "light" | "dark";

type UiState = {
  sidebarCompact: boolean;
  theme: Theme;
  toggleSidebar: () => void;
  toggleTheme: () => void;
};

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCompact: false,
      theme: "light",
      toggleSidebar: () =>
        set((state) => ({ sidebarCompact: !state.sidebarCompact })),
      toggleTheme: () =>
        set((state) => ({ theme: state.theme === "light" ? "dark" : "light" })),
    }),
    {
      name: "mcp1c-dashboard-ui",
      merge: (persisted, current) => {
        const saved = persisted as Partial<UiState>;
        return {
          ...current,
          ...saved,
          theme: saved.theme === "dark" ? "dark" : "light",
        };
      },
      partialize: (state) => ({
        sidebarCompact: state.sidebarCompact,
        theme: state.theme,
      }),
    },
  ),
);
