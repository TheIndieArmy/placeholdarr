import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    /** Listen on all interfaces so LAN URLs like http://192.168.x.x:5173 work (not just localhost). */
    host: true,
    port: 5173,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
