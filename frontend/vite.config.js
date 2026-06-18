import { defineConfig } from "vite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // Dev-only: in prod, FastAPI serves inperson.html for /demo via Host routing.
  // Locally there's no Host routing, so rewrite /demo -> the in-person shell.
  // The browser URL stays /demo, so main-inperson.jsx's wantsDemo() still fires
  // (mints the demo session + seeds the event). No effect on the prod build.
  plugins: [{
    name: "local-demo-route",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const path = (req.url || "").split("?")[0];
        if (path === "/demo" || path.startsWith("/demo/")) req.url = "/inperson.html";
        next();
      });
    },
  }],
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
    // Multi-page: two HTML shells from one build, sharing the hashed /assets.
    //   index.html    -> desktop pipeline   (surpluslayer.com)
    //   inperson.html -> phone-first capture (event.surpluslayer.com)
    // FastAPI picks which shell to serve per Host (backend/main.py).
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        inperson: resolve(__dirname, "inperson.html"),
      },
      output: {
        // Keep BookApp in its own hashed chunk (BookApp-*.js). The desktop
        // entry no longer dynamically imports it, so without this it inlines
        // into the event entry and /api/health can't fingerprint the shipped
        // book bundle (frontend_book_bundle / frontend_has_redesign go null).
        manualChunks(id) {
          if (id.includes("/BookApp.jsx")) return "BookApp";
        },
      },
    },
  },
});
