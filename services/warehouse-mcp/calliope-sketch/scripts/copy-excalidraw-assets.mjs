import { cpSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(
  projectRoot,
  "node_modules/@excalidraw/excalidraw/dist/prod/fonts",
);
const target = resolve(projectRoot, "../calliope/sketch-assets/fonts");

mkdirSync(target, { recursive: true });
cpSync(source, target, { recursive: true, force: true });
console.log(`copied Excalidraw fonts to ${target}`);
