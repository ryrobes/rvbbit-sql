import { defineConfig } from "vite";

// Artifacts are deliberately framework-free and can run in sandboxed iframes.
// Build one self-contained IIFE so authored HTML needs no package manager,
// import map, framework adapter, or network dependency at render time.
export default defineConfig({
  build: {
    lib: {
      entry: "src/entry.ts",
      name: "_RvbbitTanStackChartsBundle",
      formats: ["iife"],
      fileName: () => "rvbbit-tanstack-charts-0.3.1.js",
    },
    outDir: "../charts",
    emptyOutDir: false,
    minify: "oxc",
    sourcemap: false,
    reportCompressedSize: true,
    rollupOptions: {
      output: {
        assetFileNames: "rvbbit-tanstack-charts-0.3.1.[ext]",
      },
    },
  },
});
