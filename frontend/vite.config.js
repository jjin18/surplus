import { defineConfig } from "vite";

export default defineConfig({
  // dev server: proxy API calls to the local FastAPI on :8000 so the React
  // app can use relative paths in BOTH dev and prod. In production, FastAPI
  // serves the built frontend at the same origin so the proxy isn't needed.
  server: {
    port: 5173,
    proxy: {
      "/events":   { target: "http://localhost:8000", changeOrigin: true },
      "/webhooks": { target: "http://localhost:8000", changeOrigin: true },
      "/api":      { target: "http://localhost:8000", changeOrigin: true },
      "/docs":     { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
