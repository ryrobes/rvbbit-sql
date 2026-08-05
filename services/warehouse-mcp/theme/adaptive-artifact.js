(() => {
  "use strict";

  const dashboard = window.RVBBIT_DASHBOARD || {};
  const profile = dashboard.manifest?.design_profile;
  const behavior = profile?.behavior || profile?.tokens?.behavior || {};
  if (behavior.theme_source !== "viewer") return;

  const STORAGE_KEY = "rvbbit-warehouse-adaptive-theme-v1";
  const DB_NAME = "rvbbit-warehouse-browser";
  const DB_VERSION = 1;
  const DB_STORE = "appearance";
  const DB_KEY = "theme:default";
  const FONT_URL = "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&display=swap";
  const ROOT = document.documentElement;
  const TOKEN_KEYS = new Set([
    "--background", "--secondary-background", "--foreground", "--border", "--ring",
    "--overlay", "--main", "--main-foreground", "--chart-1", "--chart-2",
    "--chart-3", "--chart-4", "--chart-5", "--chart-6", "--success", "--warning",
    "--danger", "--info", "--chrome-bg", "--chrome-border", "--chrome-text", "--doc-bg",
    "--block-bg", "--block-bg-hover", "--block-border", "--grid-dot", "--grid-outline",
    "--rvbbit-accent", "--rvbbit-bg", "--wallpaper-overlay-from", "--wallpaper-overlay-to",
    "--ambient-1", "--ambient-2", "--ambient-3", "--void", "--panel", "--panel-raised",
    "--panel-soft", "--bone", "--bone-bright", "--fog", "--dim", "--faint", "--line",
    "--line-hot", "--amber", "--amber-soft", "--jade", "--jade-soft",
  ]);
  const fallbackTokens = profile?.tokens || {};
  const fallbackPalette = fallbackTokens.palette || {};
  const fallbackTypography = fallbackTokens.typography || {};
  let objectUrl = null;

  function safeCssValue(value, fallback = "") {
    const candidate = String(value || "").trim();
    return candidate && candidate.length <= 260 && !/[<>{};]/.test(candidate)
      ? candidate
      : fallback;
  }

  function safeFont(value, fallback) {
    const candidate = String(value || "").trim();
    return candidate && candidate.length <= 240 && !/[<>{};]|url\s*\(/i.test(candidate)
      ? candidate
      : fallback;
  }

  function safeWallpaper(value) {
    const candidate = String(value || "").trim();
    if (/^\/(?:bg\/[A-Za-z0-9_-]{1,80}\.jpg|theme\/images\/full\/[A-Za-z0-9][A-Za-z0-9_-]{0,160}\.webp)$/.test(candidate)) {
      return candidate;
    }
    return /^blob:[^\s"'<>]{1,2048}$/.test(candidate) ? candidate : null;
  }

  function cssUrl(value) {
    return `url("${String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"')}")`;
  }

  function fallbackSnapshot() {
    const palette = fallbackPalette;
    return {
      schema_version: "rvbbit.viewer-theme.v1",
      mode: "dark",
      source: { kind: "profile-fallback", label: "Adaptive Calliope fallback" },
      tokens: {
        "--background": palette.background || "#0b1218",
        "--secondary-background": palette.surface_alt || "#172731",
        "--foreground": palette.text || "#edf2f4",
        "--border": palette.border || "rgba(237,242,244,.15)",
        "--main": palette.accent || "#68c7b2",
        "--rvbbit-accent": palette.accent_alt || "#f5b446",
        "--success": palette.positive || "#67c587",
        "--warning": palette.warning || "#e8b85d",
        "--danger": palette.danger || "#e17868",
        "--panel": palette.surface || "#111d25",
        "--panel-raised": palette.surface_alt || "#172731",
        "--bone": palette.text || "#edf2f4",
        "--bone-bright": palette.text || "#edf2f4",
        "--fog": palette.muted || "#9aa8ae",
        "--dim": "rgba(237,242,244,.54)",
        "--faint": "rgba(237,242,244,.32)",
        "--line": palette.border || "rgba(237,242,244,.15)",
        "--line-hot": palette.accent || "#68c7b2",
        "--amber": palette.accent_alt || "#f5b446",
        "--jade": palette.accent || "#68c7b2",
        ...Object.fromEntries((fallbackTokens.charts?.series || []).slice(0, 6).map((color, index) => [
          `--chart-${index + 1}`,
          color,
        ])),
      },
      background: {
        mode: "solid",
        solid_color: palette.background || "#0b1218",
        wallpaper: null,
        wallpaper_opacity: .64,
        upload: false,
      },
      material: {
        glass_background: "color-mix(in oklch, var(--panel) 78%, transparent)",
        glass_border: "color-mix(in oklch, var(--bone) 15%, transparent)",
        backdrop_blur: "18px",
        shadow: fallbackTokens.effects?.shadow || "0 20px 64px rgba(0,0,0,.32)",
      },
    };
  }

  function normalizeSnapshot(value) {
    const input = value && typeof value === "object" ? value : fallbackSnapshot();
    const base = fallbackSnapshot();
    const tokens = { ...base.tokens };
    if (input.tokens && typeof input.tokens === "object") {
      Object.entries(input.tokens).forEach(([key, raw]) => {
        if (!TOKEN_KEYS.has(key)) return;
        const candidate = safeCssValue(raw);
        if (candidate) tokens[key] = candidate;
      });
    }
    const background = input.background && typeof input.background === "object"
      ? input.background
      : base.background;
    const material = input.material && typeof input.material === "object"
      ? input.material
      : base.material;
    const opacity = Number(background.wallpaper_opacity);
    return {
      mode: input.mode === "light" ? "light" : "dark",
      source: input.source && typeof input.source === "object" ? input.source : base.source,
      tokens,
      background: {
        mode: background.mode === "image" ? "image" : "solid",
        solid_color: safeCssValue(background.solid_color, base.background.solid_color),
        wallpaper: safeWallpaper(background.wallpaper),
        wallpaper_opacity: Number.isFinite(opacity) ? Math.max(.18, Math.min(1, opacity)) : .64,
        upload: Boolean(background.upload),
      },
      material: {
        glass_background: safeCssValue(material.glass_background, base.material.glass_background),
        glass_border: safeCssValue(material.glass_border, base.material.glass_border),
        backdrop_blur: safeCssValue(material.backdrop_blur, base.material.backdrop_blur),
        shadow: safeCssValue(material.shadow, base.material.shadow),
      },
    };
  }

  function value(tokens, key, fallback) {
    return safeCssValue(tokens[key], fallback);
  }

  function applySnapshot(raw) {
    const snapshot = normalizeSnapshot(raw);
    Object.entries(snapshot.tokens).forEach(([key, token]) => {
      if (TOKEN_KEYS.has(key)) ROOT.style.setProperty(key, token);
    });
    const tokens = snapshot.tokens;
    const aliases = {
      "--artifact-bg": value(tokens, "--background", fallbackPalette.background || "#0b1218"),
      "--artifact-panel": value(tokens, "--panel", fallbackPalette.surface || "#111d25"),
      "--artifact-panel-raised": value(tokens, "--panel-raised", fallbackPalette.surface_alt || "#172731"),
      "--artifact-panel-soft": value(tokens, "--panel-soft", "color-mix(in oklch, var(--artifact-panel) 76%, transparent)"),
      "--artifact-text": value(tokens, "--foreground", fallbackPalette.text || "#edf2f4"),
      "--artifact-muted": value(tokens, "--fog", fallbackPalette.muted || "#9aa8ae"),
      "--artifact-faint": value(tokens, "--dim", "color-mix(in oklch, var(--artifact-text) 38%, transparent)"),
      "--artifact-border": value(tokens, "--line", fallbackPalette.border || "rgba(237,242,244,.15)"),
      "--artifact-border-hot": value(tokens, "--line-hot", "color-mix(in oklch, var(--artifact-accent) 52%, transparent)"),
      "--artifact-accent": value(tokens, "--main", fallbackPalette.accent || "#68c7b2"),
      "--artifact-accent-alt": value(tokens, "--rvbbit-accent", fallbackPalette.accent_alt || "#f5b446"),
      "--artifact-positive": value(tokens, "--success", fallbackPalette.positive || "#67c587"),
      "--artifact-warning": value(tokens, "--warning", fallbackPalette.warning || "#e8b85d"),
      "--artifact-danger": value(tokens, "--danger", fallbackPalette.danger || "#e17868"),
      "--artifact-glass-bg": snapshot.material.glass_background,
      "--artifact-glass-border": snapshot.material.glass_border,
      "--artifact-shadow": snapshot.material.shadow,
      "--artifact-backdrop-blur": snapshot.material.backdrop_blur,
      "--artifact-font-display": safeFont(fallbackTypography.display, '"Newsreader", Georgia, serif'),
      "--artifact-font-body": safeFont(fallbackTypography.body, '"IBM Plex Sans", Inter, ui-sans-serif, sans-serif'),
      "--artifact-font-mono": safeFont(fallbackTypography.mono, '"IBM Plex Mono", ui-monospace, monospace'),
    };
    for (let index = 1; index <= 6; index += 1) {
      aliases[`--artifact-chart-${index}`] = value(
        tokens,
        `--chart-${index}`,
        fallbackTokens.charts?.series?.[index - 1] || aliases["--artifact-accent"],
      );
    }
    const wallpaper = snapshot.background.mode === "image"
      ? safeWallpaper(snapshot.background.wallpaper)
      : null;
    aliases["--artifact-wallpaper"] = wallpaper ? cssUrl(wallpaper) : "none";
    aliases["--artifact-wallpaper-opacity"] = String(snapshot.background.wallpaper_opacity);
    aliases["--artifact-solid-background"] = snapshot.background.solid_color;
    Object.entries(aliases).forEach(([key, token]) => ROOT.style.setProperty(key, token));
    ROOT.dataset.rvbbitAdaptiveTheme = "viewer";
    ROOT.dataset.rvbbitColorMode = snapshot.mode;
    ROOT.dataset.rvbbitBackgroundMode = snapshot.background.mode;
    ROOT.style.colorScheme = snapshot.mode;
    ROOT.style.backgroundColor = snapshot.background.mode === "solid"
      ? snapshot.background.solid_color
      : aliases["--artifact-bg"];
    window.dispatchEvent(new CustomEvent("rvbbit:adaptive-theme", {
      detail: { mode: snapshot.mode, source: snapshot.source },
    }));
  }

  function installDesignSystem() {
    if (!document.querySelector('link[data-rvbbit-adaptive-fonts]')) {
      const fonts = document.createElement("link");
      fonts.rel = "stylesheet";
      fonts.href = FONT_URL;
      fonts.dataset.rvbbitAdaptiveFonts = "true";
      document.head?.append(fonts);
    }
    if (document.getElementById("rvbbit-adaptive-design-system")) return;
    const style = document.createElement("style");
    style.id = "rvbbit-adaptive-design-system";
    style.textContent = `
      html[data-rvbbit-adaptive-theme="viewer"] {
        position: relative;
        min-height: 100%;
        background-color: var(--artifact-bg, #0b1218);
        isolation: isolate;
      }
      html[data-rvbbit-adaptive-theme="viewer"]::before,
      html[data-rvbbit-adaptive-theme="viewer"]::after {
        content: "";
        position: fixed;
        z-index: 0;
        inset: 0;
        pointer-events: none;
      }
      html[data-rvbbit-adaptive-theme="viewer"]::before {
        background-image: var(--artifact-wallpaper, none);
        background-position: center;
        background-size: cover;
        opacity: var(--artifact-wallpaper-opacity, .64);
      }
      html[data-rvbbit-adaptive-theme="viewer"]::after {
        background: transparent;
      }
      html[data-rvbbit-adaptive-theme="viewer"][data-rvbbit-background-mode="image"]::after {
        background: linear-gradient(to bottom,
          var(--wallpaper-overlay-from, rgba(4,8,12,.72)),
          var(--wallpaper-overlay-to, rgba(11,18,24,.52)));
      }
      html[data-rvbbit-adaptive-theme="viewer"] body {
        position: relative;
        z-index: 1;
        min-height: 100%;
        background-color: transparent !important;
        color: var(--artifact-text, #edf2f4);
        font-family: var(--artifact-font-body, "IBM Plex Sans", sans-serif);
        font-feature-settings: "tnum" 1, "ss01" 1;
        text-rendering: optimizeLegibility;
      }
      html[data-rvbbit-adaptive-theme="viewer"] :is(h1,h2,.display,.editorial-title) {
        font-family: var(--artifact-font-display, "Newsreader", Georgia, serif);
      }
      html[data-rvbbit-adaptive-theme="viewer"] :is(code,pre,kbd,samp,.mono,.eyebrow,.kicker) {
        font-family: var(--artifact-font-mono, "IBM Plex Mono", monospace);
      }
      @media (prefers-reduced-motion: reduce) {
        html[data-rvbbit-adaptive-theme="viewer"] *,
        html[data-rvbbit-adaptive-theme="viewer"] *::before,
        html[data-rvbbit-adaptive-theme="viewer"] *::after {
          scroll-behavior: auto !important;
          animation-duration: .01ms !important;
          animation-iteration-count: 1 !important;
          transition-duration: .01ms !important;
        }
      }
    `;
    document.head?.append(style);
  }

  function readStoredSnapshot() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    } catch {
      return null;
    }
  }

  function loadUploadBlob() {
    return new Promise((resolve) => {
      if (!window.indexedDB) return resolve(null);
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(DB_STORE)) {
          request.result.createObjectStore(DB_STORE, { keyPath: "id" });
        }
      };
      request.onerror = () => resolve(null);
      request.onsuccess = () => {
        const db = request.result;
        const transaction = db.transaction(DB_STORE, "readonly");
        const get = transaction.objectStore(DB_STORE).get(DB_KEY);
        get.onerror = () => resolve(null);
        get.onsuccess = () => resolve(get.result?.blob || null);
        transaction.oncomplete = () => db.close();
        transaction.onerror = () => db.close();
      };
    });
  }

  async function applyDirectSnapshot() {
    const stored = readStoredSnapshot();
    if (!stored) return;
    if (stored.background?.upload && !stored.background?.wallpaper) {
      const blob = await loadUploadBlob();
      if (blob) {
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = URL.createObjectURL(blob);
        stored.background = { ...stored.background, wallpaper: objectUrl };
      }
    }
    applySnapshot(stored);
  }

  installDesignSystem();
  applySnapshot(fallbackSnapshot());
  if (window.parent !== window) {
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent || event.data?.type !== "rvbbit.adaptive-theme.apply") return;
      applySnapshot(event.data.snapshot);
    });
    window.parent.postMessage({ type: "rvbbit.adaptive-theme.request" }, "*");
  } else {
    void applyDirectSnapshot();
    window.addEventListener("storage", (event) => {
      if (event.key === STORAGE_KEY) void applyDirectSnapshot();
    });
  }
  window.addEventListener("pagehide", () => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  });
})();
