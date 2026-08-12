export type ThemeMode = "dark" | "light";

// Keep this bridge deliberately smaller than the full Warehouse theme. These
// are the color roles the Sketch chrome consumes; wallpaper and font settings
// remain owned by the surrounding Calliope room.
const ADAPTIVE_COLOR_TOKENS = [
  "--background",
  "--secondary-background",
  "--foreground",
  "--border",
  "--ring",
  "--overlay",
  "--main",
  "--main-foreground",
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--chart-5",
  "--chart-6",
  "--success",
  "--warning",
  "--danger",
  "--info",
  "--chrome-bg",
  "--chrome-border",
  "--chrome-text",
  "--doc-bg",
  "--block-bg",
  "--block-bg-hover",
  "--block-border",
  "--grid-dot",
  "--grid-outline",
  "--rvbbit-accent",
  "--rvbbit-bg",
  "--void",
  "--panel",
  "--panel-raised",
  "--panel-soft",
  "--bone",
  "--bone-bright",
  "--fog",
  "--dim",
  "--faint",
  "--line",
  "--line-hot",
  "--amber",
  "--amber-soft",
  "--jade",
  "--jade-soft",
] as const;

function safeTokenValue(value: unknown) {
  const text = String(value || "").trim();
  if (!text || text.length > 240 || /[<>{};]|url\s*\(/i.test(text)) return "";
  return text;
}

export function themeModeFromPayload(value: any): ThemeMode {
  const mode = value?.mode
    || value?.snapshot?.mode
    || value?.snapshot?.color_mode
    || value?.snapshot?.colorMode;
  return mode === "light" ? "light" : "dark";
}

export function applyAdaptiveSketchTheme(
  value: any,
  root: HTMLElement = document.documentElement,
): ThemeMode {
  const mode = themeModeFromPayload(value);
  const tokens = value?.snapshot?.tokens;
  let applied = 0;
  for (const key of ADAPTIVE_COLOR_TOKENS) {
    root.style.removeProperty(key);
    const token = safeTokenValue(tokens?.[key]);
    if (!token) continue;
    root.style.setProperty(key, token);
    applied += 1;
  }
  root.dataset.theme = mode;
  root.dataset.adaptiveTheme = applied ? "true" : "fallback";
  return mode;
}
