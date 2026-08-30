import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const developmentApiTarget =
  process.env.VITE_DEV_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("maplibre-gl") || id.includes("h3-js")) return "geo";
          if (id.includes("hls.js")) return "streaming";
          if (id.includes("gsap") || id.includes("@gsap")) return "gsap";
          if (id.includes("motion")) return "motion";
          if (id.includes("@tanstack")) return "query";
          if (id.includes("react")) return "react";
          return "vendor";
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/v1": developmentApiTarget,
      "/health": developmentApiTarget,
      "/ready": developmentApiTarget,
    },
  },
});
