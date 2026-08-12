import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The sketch is an isolated same-origin island inside the otherwise vanilla
// Calliope notebook. React and Excalidraw are bundled deliberately so the
// editor cannot perturb the parent page's dependency or CSS lifecycle.
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    lib: {
      entry: "src/main.tsx",
      name: "RvbbitCalliopeSketch",
      formats: ["iife"],
      fileName: () => "sketch-runtime.js"
    },
    cssCodeSplit: false,
    outDir: "../calliope",
    emptyOutDir: false,
    sourcemap: false,
    rollupOptions: {
      output: {
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith(".css"))
            ? "sketch-runtime.css"
            : "sketch-assets/[name]-[hash][extname]"
      }
    }
  }
});
