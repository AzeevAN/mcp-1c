import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "MCP1C_");
  const apiOrigin = env.MCP1C_API_ORIGIN || "http://127.0.0.1:5003";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": apiOrigin,
        "/health": apiOrigin,
        "/login": apiOrigin,
        "/logout": apiOrigin,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      css: true,
    },
  };
});
