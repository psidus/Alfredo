import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built app is served by FastAPI under /app (see core/api_server.py), and the
// same base keeps `npm run dev` / `npm run preview` URLs consistent.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
