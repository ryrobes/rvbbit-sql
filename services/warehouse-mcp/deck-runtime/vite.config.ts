import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// One self-contained IIFE + one CSS file. React/framer-motion are bundled
// on purpose: decks render inside an iframe at /d/<slug>, so isolation is
// the boundary and portability beats deduplication. P0 inlines both files
// into the published deck HTML; P1 serves them as pinned shared assets.
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    lib: {
      entry: "src/entry.tsx",
      name: "RvbbitDeck",
      formats: ["iife"],
      fileName: () => "deck-runtime.js"
    },
    cssCodeSplit: false,
    outDir: "dist",
    rollupOptions: {
      output: { assetFileNames: "deck-runtime.[ext]" }
    }
  }
});
