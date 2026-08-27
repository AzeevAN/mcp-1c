import { create } from "zustand";
import { persist } from "zustand/middleware";

type UiState = {
  sidebarCompact: boolean;
  toggleSidebar: () => void;
};

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCompact: false,
      toggleSidebar: () =>
        set((state) => ({ sidebarCompact: !state.sidebarCompact })),
    }),
    {
      name: "mcp1c-dashboard-ui",
      partialize: (state) => ({ sidebarCompact: state.sidebarCompact }),
    },
  ),
);
