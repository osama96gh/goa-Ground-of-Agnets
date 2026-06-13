import { fileURLToPath, URL } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Read the workspace .env (one level up from goa-dashboard/) so the dev
  // dashboard can share a single source of truth for GOA_ADMIN_TOKEN with
  // the hub. `..` is resolved relative to where `vite` is invoked, which is
  // always the goa-dashboard/ root via `npm run dev` / `npm run build`.
  //
  // The empty prefix returns ALL keys (not just VITE_*); we then forward
  // only the keys we want exposed to the client via `define` below.
  const env = loadEnv(mode, "..", "");
  const devAdminToken = env.GOA_ADMIN_TOKEN ?? "";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      port: 5173,
      proxy: {
        // Proxy admin API calls to the hub in dev so the dashboard origin matches
        // (avoids preflight CORS for SSE during development). Same-origin in prod.
        // Only /admin is proxied: other paths (/tasks, /participants, ...) are
        // SPA routes that must fall back to index.html on hard refresh.
        "/admin": { target: "http://127.0.0.1:8000", ws: false },
      },
    },
    define: {
      // Inlined at build time. Used in dev to auto-fill the admin token gate
      // so `make demo` opens a usable dashboard with no manual paste. The
      // PROD bundle gets `""` here (Vite tree-shakes the dev branch), so the
      // dev-only token never ships to a production deployment.
      "import.meta.env.VITE_GOA_ADMIN_TOKEN": JSON.stringify(
        mode === "development" ? devAdminToken : "",
      ),
    },
  };
});
