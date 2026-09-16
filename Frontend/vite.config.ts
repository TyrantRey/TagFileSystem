import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The daemon serves Frontend/dist at /ui/ (DESIGN/v0-5-0.md §3.3). `vite`
// (npm run dev) serves the same app itself and proxies /api to a daemon, so
// the `tfs ui` address works on the dev server too: swap the origin, keep
// the #token=... fragment. TFS_DAEMON overrides the daemon's address.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: { "/api": process.env.TFS_DAEMON ?? "http://127.0.0.1:7411" },
  },
  test: {
    environment: "happy-dom",
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
