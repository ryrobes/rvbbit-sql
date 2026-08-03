import { Vibrant } from "node-vibrant/browser";

/*
 * DataRabbit's image-theme pipeline, adapted to the framework-free Warehouse:
 *
 *   image -> node-vibrant role swatches -> ImagePalette -> dark/light UI tokens
 *
 * Library choices keep a stable source URL in localStorage.  Custom uploads
 * keep the actual Blob in IndexedDB, while their compact palette/tokens live in
 * localStorage so colors can be restored synchronously in <head>.  The script
 * is deliberately included only by first-party Warehouse shells.
 */

const STORAGE_KEY = "rvbbit-warehouse-theme-v1";
const MODE_STORAGE_KEY = "rvbbit-warehouse-color-mode";
const DB_NAME = "rvbbit-warehouse-browser";
const DB_VERSION = 1;
const DB_STORE = "appearance";
const DB_KEY = "theme:default";
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const LIBRARY_URL = /^\/theme\/images\/full\/[A-Za-z0-9][A-Za-z0-9_-]{0,160}\.webp$/;
const SOLID_COLOR = /^#[0-9a-f]{6}$/i;
const ROOT = document.documentElement;
const SUN_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.5"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"></path></svg>';
const MOON_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.2 15.3A8.5 8.5 0 0 1 8.7 3.8 8.5 8.5 0 1 0 20.2 15.3Z"></path></svg>';

const THEME_KEYS = [
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
  "--wallpaper-overlay-from",
  "--wallpaper-overlay-to",
  "--ambient-1",
  "--ambient-2",
  "--ambient-3",
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
  "--warehouse-wallpaper",
  "--warehouse-solid-background",
];

let colorMode = readColorMode();
let appliedTokens = null;
let current = readStoredState();
let selectedBackgroundMode = themeBackground(current).mode;
let selectedSolidColor = themeBackground(current).solidColor;
let solidColorTouched = Boolean(current?.background?.solidColor);
let previewWallpaperUrl = null;
let activeObjectUrl = null;
let button = null;
let modeButton = null;
let dialog = null;
let library = null;
let selectedItem = null;
let selectedPalette = null;
let selectedPalettePromise = null;
let previewSequence = 0;

applyColorModeRoot();
applyStoredState(current);

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}

function init() {
  installButton();
  installDialog();
  refreshButton();
  if (current?.source?.kind === "upload") {
    void restoreUploadImage(current);
  }
  window.addEventListener("storage", onStorage);
  window.addEventListener("pagehide", releaseObjectUrl);
  window.WarehouseTheme = Object.freeze({
    open: openDialog,
    reset: resetTheme,
    getState: () => current,
    getMode: () => colorMode,
    setMode: (mode) => setColorMode(mode),
  });
}

function installButton() {
  const anchor = document.querySelector("[data-warehouse-theme-anchor]");
  const host = anchor || document.createElement("span");
  if (!anchor) {
    host.className = "warehouse-theme-fallback";
    document.body.append(host);
  }
  button = document.createElement("button");
  button.type = "button";
  button.className = "warehouse-theme-button";
  button.title = "Warehouse appearance";
  button.setAttribute("aria-label", "Choose warehouse appearance");
  button.setAttribute("aria-haspopup", "dialog");
  button.setAttribute("aria-expanded", "false");
  button.innerHTML = '<span class="warehouse-theme-button-thumb" aria-hidden="true"></span>';
  button.addEventListener("click", openDialog);

  modeButton = document.createElement("button");
  modeButton.type = "button";
  modeButton.className = "warehouse-theme-mode-button";
  modeButton.addEventListener("click", toggleColorMode);
  refreshModeButton();
  host.append(button, modeButton);
}

function readColorMode() {
  try {
    return localStorage.getItem(MODE_STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function applyColorModeRoot() {
  ROOT.dataset.theme = colorMode;
  ROOT.dataset.warehouseColorMode = colorMode;
  ROOT.style.colorScheme = colorMode;
  ROOT.classList.toggle("dark", colorMode === "dark");
}

function toggleColorMode() {
  setColorMode(colorMode === "dark" ? "light" : "dark");
}

function setColorMode(mode, { persist = true } = {}) {
  const next = mode === "light" ? "light" : "dark";
  if (next === colorMode) {
    refreshModeButton();
    return colorMode;
  }
  colorMode = next;
  if (persist) {
    try {
      localStorage.setItem(MODE_STORAGE_KEY, colorMode);
    } catch {
      // The mode still applies for this page when persistence is blocked.
    }
  }
  clearAppliedTheme();
  applyColorModeRoot();
  applyStoredState(current, activeObjectUrl);
  refreshButton();
  refreshModeButton();
  showCurrentPreview();
  notifyThemeChange();
  return colorMode;
}

function refreshModeButton() {
  if (!modeButton) return;
  const toLight = colorMode === "dark";
  const label = toLight ? "Switch to light mode" : "Switch to dark mode";
  modeButton.innerHTML = toLight ? SUN_ICON : MOON_ICON;
  modeButton.title = label;
  modeButton.setAttribute("aria-label", label);
  modeButton.setAttribute("aria-pressed", String(colorMode === "light"));
  modeButton.dataset.mode = colorMode;
}

function installDialog() {
  dialog = document.createElement("dialog");
  dialog.className = "warehouse-theme-dialog";
  dialog.setAttribute("aria-labelledby", "warehouse-theme-title");
  dialog.innerHTML = `
    <div class="warehouse-theme-shell">
      <header class="warehouse-theme-head">
        <div>
          <p class="warehouse-theme-kicker">Browser appearance</p>
          <h2 id="warehouse-theme-title">Choose a room</h2>
        </div>
        <button class="warehouse-theme-close" type="button" aria-label="Close appearance picker">×</button>
      </header>
      <div class="warehouse-theme-content">
        <section class="warehouse-theme-library" aria-label="Theme image library">
          <div class="warehouse-theme-empty" data-theme-status>Loading image library…</div>
          <div class="warehouse-theme-grid" data-theme-grid hidden></div>
        </section>
        <aside class="warehouse-theme-preview">
          <div class="warehouse-theme-preview-image" data-theme-preview-image></div>
          <h3 data-theme-preview-title>Current warehouse background</h3>
          <p data-theme-preview-copy>Choose an image. Data Rabbit will derive the interface and chart colors automatically.</p>
          <div class="warehouse-theme-swatches" data-theme-swatches aria-label="Derived color palette"></div>
          <div class="warehouse-theme-background">
            <div class="warehouse-theme-background-head">
              <b>Page background</b>
              <span>The theme palette stays active</span>
            </div>
            <div class="warehouse-theme-background-modes" aria-label="Page background style">
              <button type="button" data-theme-background-mode="image" aria-pressed="true">Wallpaper</button>
              <button type="button" data-theme-background-mode="solid" aria-pressed="false">Solid color</button>
            </div>
            <label class="warehouse-theme-color" data-theme-solid-picker hidden>
              <span>Background color</span>
              <input type="color" data-theme-solid-color value="#100d0b">
              <output data-theme-solid-value>#100d0b</output>
            </label>
          </div>
          <div class="warehouse-theme-error" data-theme-error hidden></div>
          <div class="warehouse-theme-actions">
            <label class="warehouse-theme-upload">
              <input type="file" accept="image/*,.avif,.jpg,.jpeg,.png,.webp">
              Upload image
            </label>
            <button class="warehouse-theme-action" type="button" data-theme-reset>Use default</button>
            <button class="warehouse-theme-action primary" type="button" data-theme-apply disabled>Apply</button>
          </div>
          <p class="warehouse-theme-footnote">Saved in this browser. Published HTML and JavaScript artifacts keep their own styling.</p>
        </aside>
      </div>
    </div>`;
  document.body.append(dialog);

  dialog.querySelector(".warehouse-theme-close").addEventListener("click", closeDialog);
  dialog.querySelector("[data-theme-reset]").addEventListener("click", () => void resetTheme());
  dialog.querySelector("[data-theme-apply]").addEventListener("click", () => void applySelectedTheme());
  dialog.querySelector('input[type="file"]').addEventListener("change", onUpload);
  dialog.querySelectorAll("[data-theme-background-mode]").forEach((control) => {
    control.addEventListener("click", () => setBackgroundMode(control.dataset.themeBackgroundMode));
  });
  dialog.querySelector("[data-theme-solid-color]").addEventListener("input", onSolidColor);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeDialog();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeDialog();
  });
}

function openDialog() {
  if (!dialog) return;
  button?.setAttribute("aria-expanded", "true");
  setError("");
  dialog.showModal();
  showCurrentPreview();
  if (library) {
    renderLibrary();
  } else {
    void loadLibrary();
  }
}

function closeDialog() {
  if (dialog?.open) dialog.close();
  button?.setAttribute("aria-expanded", "false");
}

async function loadLibrary() {
  const status = dialog.querySelector("[data-theme-status]");
  try {
    const response = await fetch("/theme/library", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Image library failed (${response.status})`);
    const payload = await response.json();
    library = Array.isArray(payload.items) ? payload.items.filter(validLibraryItem) : [];
    renderLibrary();
  } catch (error) {
    status.hidden = false;
    status.textContent = error instanceof Error ? error.message : "Image library unavailable.";
  }
}

function validLibraryItem(item) {
  return Boolean(
    item
    && /^[A-Za-z0-9][A-Za-z0-9_-]{0,160}$/.test(String(item.id || ""))
    && LIBRARY_URL.test(String(item.url || ""))
    && /^\/theme\/images\/thumb\/[A-Za-z0-9][A-Za-z0-9_-]{0,160}\.webp$/.test(String(item.thumb || "")),
  );
}

function renderLibrary() {
  const status = dialog.querySelector("[data-theme-status]");
  const grid = dialog.querySelector("[data-theme-grid]");
  status.hidden = Boolean(library?.length);
  grid.hidden = !library?.length;
  if (!library?.length) {
    status.textContent = "No bundled images are available.";
    return;
  }
  const activeId = selectedItem?.id || (current?.source?.kind === "library" ? current.source.id : "");
  grid.innerHTML = library.map((item) => `
    <button class="warehouse-theme-tile" type="button" data-theme-id="${escapeHtml(item.id)}"
            aria-pressed="${String(item.id === activeId)}" title="${escapeHtml(item.label)}">
      <img src="${escapeHtml(item.thumb)}" alt="" loading="lazy" decoding="async">
      <span>${escapeHtml(item.label)}</span>
    </button>`).join("");
  grid.querySelectorAll("[data-theme-id]").forEach((tile) => {
    tile.addEventListener("click", () => selectLibraryItem(tile.dataset.themeId));
  });
}

function selectLibraryItem(id) {
  const item = library?.find((candidate) => candidate.id === id);
  if (!item) return;
  selectedItem = item;
  selectedPalette = current?.source?.kind === "library" && current.source.id === item.id
    ? current.palette
    : null;
  if (!solidColorTouched && selectedPalette) {
    selectedSolidColor = derivedSolidColor(selectedPalette);
  }
  renderLibrary();
  renderPreview(item.url, item.label, selectedPalette);
  syncBackgroundControls();
  const apply = dialog.querySelector("[data-theme-apply]");
  apply.disabled = false;
  apply.textContent = selectedPalette ? "Apply" : "Deriving…";
  const sequence = ++previewSequence;
  selectedPalettePromise = selectedPalette
    ? Promise.resolve(selectedPalette)
    : extractPalette(item.url).then((palette) => ({ ...palette, source: item.label }));
  selectedPalettePromise.then((palette) => {
    if (sequence !== previewSequence || selectedItem?.id !== item.id) return;
    selectedPalette = palette;
    if (!solidColorTouched) selectedSolidColor = derivedSolidColor(palette);
    renderSwatches(palette);
    syncBackgroundControls();
    apply.textContent = "Apply";
  }).catch((error) => {
    if (sequence !== previewSequence) return;
    selectedPalettePromise = null;
    apply.textContent = "Apply";
    setError(error instanceof Error ? error.message : "Could not derive colors from that image.");
  });
}

async function applySelectedTheme() {
  if (!selectedItem && !current) return;
  const apply = dialog.querySelector("[data-theme-apply]");
  setBusy(true);
  setError("");
  try {
    let next;
    let uploadUrl = activeObjectUrl;
    if (selectedItem) {
      const palette = selectedPalette || await (
        selectedPalettePromise || extractPalette(selectedItem.url)
      );
      next = {
        version: 1,
        source: {
          kind: "library",
          id: selectedItem.id,
          label: selectedItem.label,
          url: selectedItem.url,
        },
        palette: { ...palette, source: selectedItem.label },
        tokens: deriveWarehouseTokens(palette),
        background: backgroundChoice(),
        updatedAt: new Date().toISOString(),
      };
      await clearUploadBlob().catch(() => {});
      uploadUrl = null;
    } else {
      next = {
        ...current,
        background: backgroundChoice(),
        updatedAt: new Date().toISOString(),
      };
      if (
        next.source.kind === "upload"
        && selectedBackgroundMode === "image"
        && !uploadUrl
      ) {
        const blob = await loadUploadBlob();
        if (blob) {
          releaseObjectUrl();
          activeObjectUrl = URL.createObjectURL(blob);
          uploadUrl = activeObjectUrl;
        }
      }
    }
    persistAndApply(next, uploadUrl);
    closeDialog();
  } catch (error) {
    setError(error instanceof Error ? error.message : "Could not apply that image.");
  } finally {
    setBusy(false);
    apply.textContent = "Apply";
  }
}

async function onUpload(event) {
  const input = event.currentTarget;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    setError("Choose an image file.");
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    setError("That image is larger than 20 MB.");
    return;
  }
  setBusy(true);
  setError("");
  let url = null;
  try {
    url = URL.createObjectURL(file);
    const palette = await extractPalette(url);
    await saveUploadBlob(file);
    const next = {
      version: 1,
      source: { kind: "upload", name: file.name },
      palette: { ...palette, source: file.name },
      tokens: deriveWarehouseTokens(palette),
      background: backgroundChoice(),
      updatedAt: new Date().toISOString(),
    };
    releaseObjectUrl();
    activeObjectUrl = url;
    url = null;
    persistAndApply(next, activeObjectUrl);
    closeDialog();
  } catch (error) {
    setError(error instanceof Error ? error.message : "Could not save that image.");
  } finally {
    if (url) URL.revokeObjectURL(url);
    setBusy(false);
  }
}

async function resetTheme() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The visible reset still works even when persistence is blocked.
  }
  await clearUploadBlob().catch(() => {});
  releaseObjectUrl();
  current = null;
  clearAppliedTheme();
  refreshButton();
  selectedItem = null;
  selectedPalette = null;
  selectedPalettePromise = null;
  selectedBackgroundMode = "image";
  selectedSolidColor = derivedSolidColor(null);
  solidColorTouched = false;
  showCurrentPreview();
  renderLibrary();
  notifyThemeChange();
  closeDialog();
}

function persistAndApply(next, uploadUrl = null) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  current = next;
  clearAppliedTheme();
  applyStoredState(next, uploadUrl);
  refreshButton();
  notifyThemeChange();
}

function readStoredState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!parsed || parsed.version !== 1 || !validStoredSource(parsed.source)) return null;
    if (!parsed.tokens || typeof parsed.tokens !== "object") return null;
    return parsed;
  } catch {
    return null;
  }
}

function validStoredSource(source) {
  if (source?.kind === "upload") return typeof source.name === "string";
  return source?.kind === "library"
    && /^[A-Za-z0-9][A-Za-z0-9_-]{0,160}$/.test(String(source.id || ""))
    && LIBRARY_URL.test(String(source.url || ""));
}

function validStoredPalette(palette) {
  if (!palette || !Number.isFinite(Number(palette.baseHue)) || !Number.isFinite(Number(palette.chroma))) {
    return false;
  }
  return ["vibrant", "darkVibrant", "lightVibrant"].every((key) => (
    typeof palette[key] === "string" && palette[key].length <= 180
  ));
}

function applyStoredState(state, uploadUrl = null) {
  appliedTokens = null;
  if (!state) return;
  const tokens = validStoredPalette(state.palette)
    ? deriveWarehouseTokens(state.palette, colorMode)
    : state.tokens;
  appliedTokens = tokens;
  for (const key of THEME_KEYS) {
    if (key === "--warehouse-wallpaper" || key === "--warehouse-solid-background") continue;
    const value = tokens?.[key];
    if (typeof value === "string" && value.length <= 180) {
      ROOT.style.setProperty(key, value);
    }
  }
  const background = themeBackground(state);
  ROOT.style.setProperty("--warehouse-solid-background", background.solidColor);
  ROOT.dataset.warehouseBackground = background.mode;
  const wallpaper = state.source.kind === "library" ? state.source.url : uploadUrl;
  if (background.mode === "image" && wallpaper) setWallpaper(wallpaper);
  ROOT.dataset.warehouseTheme = state.source.kind;
}

function clearAppliedTheme() {
  for (const key of THEME_KEYS) ROOT.style.removeProperty(key);
  appliedTokens = null;
  delete ROOT.dataset.warehouseTheme;
  delete ROOT.dataset.warehouseWallpaper;
  delete ROOT.dataset.warehouseBackground;
}

function setWallpaper(url) {
  const escaped = String(url).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
  ROOT.style.setProperty("--warehouse-wallpaper", `url("${escaped}")`);
  ROOT.dataset.warehouseWallpaper = "true";
}

async function restoreUploadImage(state) {
  try {
    const blob = await loadUploadBlob();
    if (!blob || current !== state) return;
    releaseObjectUrl();
    activeObjectUrl = URL.createObjectURL(blob);
    if (themeBackground(state).mode === "image") setWallpaper(activeObjectUrl);
    refreshButton();
  } catch {
    // Keep the derived colors. The reset action can clear a stale record.
  }
}

function releaseObjectUrl() {
  if (!activeObjectUrl) return;
  URL.revokeObjectURL(activeObjectUrl);
  activeObjectUrl = null;
}

function refreshButton() {
  if (!button) return;
  button.dataset.hasTheme = String(Boolean(current));
  button.dataset.backgroundMode = current ? themeBackground(current).mode : "default";
  const thumb = button.querySelector(".warehouse-theme-button-thumb");
  if (thumb) {
    thumb.style.backgroundImage = "";
    if (!current) {
      const fallback = document.querySelector(".bg");
      const fallbackImage = fallback ? getComputedStyle(fallback).backgroundImage : "";
      if (fallbackImage && fallbackImage !== "none") {
        thumb.style.backgroundImage = fallbackImage;
      }
    }
  }
  button.title = current
    ? `Appearance · ${current.source.label || current.source.name || "custom image"} · ${
      themeBackground(current).mode === "solid" ? "solid background" : "wallpaper"
    }`
    : "Warehouse appearance";
}

function showCurrentPreview() {
  if (!dialog) return;
  selectedItem = current?.source?.kind === "library" && library
    ? library.find((item) => item.id === current.source.id) || null
    : null;
  selectedPalette = current?.palette || null;
  selectedPalettePromise = selectedPalette ? Promise.resolve(selectedPalette) : null;
  const background = themeBackground(current);
  selectedBackgroundMode = background.mode;
  selectedSolidColor = background.solidColor;
  solidColorTouched = Boolean(current?.background?.solidColor);
  const url = current?.source?.kind === "library" ? current.source.url : activeObjectUrl;
  const label = current?.source?.label || current?.source?.name || "Current warehouse background";
  renderPreview(url, label, current?.palette || null);
  if (!url && selectedBackgroundMode === "image") {
    const fallback = document.querySelector(".bg");
    const fallbackImage = fallback ? getComputedStyle(fallback).backgroundImage : "";
    if (fallbackImage && fallbackImage !== "none") {
      dialog.querySelector("[data-theme-preview-image]").style.backgroundImage = fallbackImage;
    }
  }
  dialog.querySelector("[data-theme-reset]").disabled = !current;
  dialog.querySelector("[data-theme-apply]").disabled = !selectedItem && !current;
  syncBackgroundControls();
}

function renderPreview(url, label, palette) {
  previewWallpaperUrl = url || null;
  const image = dialog.querySelector("[data-theme-preview-image]");
  dialog.querySelector("[data-theme-preview-title]").textContent = label;
  renderSwatches(palette);
  syncBackgroundControls();
}

function renderSwatches(palette) {
  const row = dialog.querySelector("[data-theme-swatches]");
  if (!palette) {
    row.innerHTML = "";
    return;
  }
  row.innerHTML = [
    palette.vibrant,
    palette.darkVibrant,
    palette.lightVibrant,
    palette.muted,
    palette.darkMuted,
    palette.lightMuted,
  ].filter(Boolean).map((color) => (
    `<span class="warehouse-theme-swatch" style="--swatch:${escapeHtml(color)}"></span>`
  )).join("");
}

function setBackgroundMode(mode) {
  if (!selectedItem && !current) return;
  if (mode !== "image" && mode !== "solid") return;
  selectedBackgroundMode = mode;
  if (mode === "solid" && !solidColorTouched) {
    selectedSolidColor = derivedSolidColor(selectedPalette || current?.palette);
  }
  syncBackgroundControls();
}

function onSolidColor(event) {
  const value = String(event.currentTarget.value || "").toLowerCase();
  if (!SOLID_COLOR.test(value)) return;
  selectedSolidColor = value;
  solidColorTouched = true;
  syncBackgroundControls();
}

function syncBackgroundControls() {
  if (!dialog) return;
  const available = Boolean(selectedItem || current);
  dialog.querySelectorAll("[data-theme-background-mode]").forEach((control) => {
    const active = control.dataset.themeBackgroundMode === selectedBackgroundMode;
    control.setAttribute("aria-pressed", String(active));
    control.disabled = !available;
  });
  const picker = dialog.querySelector("[data-theme-solid-picker]");
  picker.hidden = selectedBackgroundMode !== "solid";
  const input = dialog.querySelector("[data-theme-solid-color]");
  input.value = selectedSolidColor;
  input.disabled = !available;
  dialog.querySelector("[data-theme-solid-value]").textContent = selectedSolidColor;

  const preview = dialog.querySelector("[data-theme-preview-image]");
  preview.dataset.backgroundMode = selectedBackgroundMode;
  if (selectedBackgroundMode === "solid") {
    preview.style.backgroundImage = "none";
    preview.style.backgroundColor = selectedSolidColor;
  } else {
    preview.style.backgroundColor = "";
    preview.style.backgroundImage = previewWallpaperUrl
      ? `url("${String(previewWallpaperUrl).replaceAll('"', '\\"')}")`
      : "";
  }
}

function backgroundChoice() {
  return {
    mode: selectedBackgroundMode === "solid" ? "solid" : "image",
    solidColor: SOLID_COLOR.test(selectedSolidColor)
      ? selectedSolidColor.toLowerCase()
      : derivedSolidColor(selectedPalette || current?.palette),
  };
}

function themeBackground(state) {
  const solidColor = String(state?.background?.solidColor || "").toLowerCase();
  return {
    mode: state?.background?.mode === "solid" ? "solid" : "image",
    solidColor: SOLID_COLOR.test(solidColor)
      ? solidColor
      : derivedSolidColor(state?.palette),
  };
}

function derivedSolidColor(palette) {
  const hue = Number.isFinite(Number(palette?.baseHue))
    ? Number(palette.baseHue)
    : 28;
  return colorMode === "light"
    ? hslToHex(hue, 18, 94)
    : hslToHex(hue, 22, 7);
}

function hslToHex(hue, saturation, lightness) {
  const h = ((Number(hue) % 360) + 360) % 360;
  const s = Math.max(0, Math.min(100, Number(saturation))) / 100;
  const l = Math.max(0, Math.min(100, Number(lightness))) / 100;
  const chroma = (1 - Math.abs(2 * l - 1)) * s;
  const segment = h / 60;
  const x = chroma * (1 - Math.abs((segment % 2) - 1));
  const [r1, g1, b1] = segment < 1 ? [chroma, x, 0]
    : segment < 2 ? [x, chroma, 0]
      : segment < 3 ? [0, chroma, x]
        : segment < 4 ? [0, x, chroma]
          : segment < 5 ? [x, 0, chroma]
            : [chroma, 0, x];
  const m = l - chroma / 2;
  return `#${[r1, g1, b1].map((part) => (
    Math.round((part + m) * 255).toString(16).padStart(2, "0")
  )).join("")}`;
}

function setBusy(busy) {
  dialog.querySelector(".warehouse-theme-upload").setAttribute("aria-disabled", String(busy));
  dialog.querySelector("[data-theme-reset]").disabled = busy || !current;
  dialog.querySelector("[data-theme-apply]").disabled = busy || (!selectedItem && !current);
  dialog.querySelector('input[type="file"]').disabled = busy;
  dialog.querySelectorAll("[data-theme-background-mode]").forEach((control) => {
    control.disabled = busy || (!selectedItem && !current);
  });
  dialog.querySelector("[data-theme-solid-color]").disabled = busy || (!selectedItem && !current);
}

function setError(message) {
  if (!dialog) return;
  const error = dialog.querySelector("[data-theme-error]");
  error.hidden = !message;
  error.textContent = message;
}

function onStorage(event) {
  if (event.key === MODE_STORAGE_KEY) {
    setColorMode(readColorMode(), { persist: false });
    return;
  }
  if (event.key !== STORAGE_KEY) return;
  releaseObjectUrl();
  clearAppliedTheme();
  current = readStoredState();
  applyStoredState(current);
  refreshButton();
  showCurrentPreview();
  renderLibrary();
  if (current?.source?.kind === "upload") void restoreUploadImage(current);
  notifyThemeChange();
}

function notifyThemeChange() {
  window.dispatchEvent(new CustomEvent("warehouse-theme-change", {
    detail: {
      source: current?.source || null,
      tokens: appliedTokens,
      background: current ? themeBackground(current) : null,
      mode: colorMode,
    },
  }));
}

async function extractPalette(input) {
  const palette = await Vibrant.from(input).getPalette();
  return paletteToImagePalette(palette);
}

function paletteToImagePalette(palette) {
  const vibrant = firstNonNull(
    palette.Vibrant,
    palette.LightVibrant,
    palette.Muted,
    palette.LightMuted,
    palette.DarkVibrant,
    palette.DarkMuted,
  );
  if (!vibrant) {
    return {
      vibrant: "oklch(76% 0.14 195)",
      darkVibrant: "oklch(40% 0.10 195)",
      lightVibrant: "oklch(82% 0.10 195)",
      muted: "oklch(40% 0.03 270)",
      darkMuted: "oklch(15% 0.04 270)",
      lightMuted: "oklch(75% 0.02 270)",
      baseHue: 270,
      chroma: .04,
      source: "node-vibrant fallback",
      generatedAt: new Date().toISOString(),
    };
  }
  const darkVibrant = palette.DarkVibrant || palette.DarkMuted || darken(vibrant);
  const lightVibrant = palette.LightVibrant || palette.LightMuted || lighten(vibrant);
  const muted = palette.Muted || palette.LightMuted || palette.DarkMuted || vibrant;
  const darkMuted = palette.DarkMuted || darken(muted);
  const lightMuted = palette.LightMuted || lighten(muted);
  const baseSwatch = pickMostPopulous([
    palette.Vibrant,
    palette.DarkVibrant,
    palette.LightVibrant,
    palette.Muted,
  ]) || vibrant;
  const baseHcl = rgbToOklch(...baseSwatch.rgb);
  const chromaSamples = [
    palette.Vibrant,
    palette.LightVibrant,
    palette.DarkVibrant,
  ].filter(Boolean).map((swatch) => rgbToOklch(...swatch.rgb).c);
  const chroma = chromaSamples.length
    ? chromaSamples.reduce((sum, value) => sum + value, 0) / chromaSamples.length
    : baseHcl.c;

  return {
    vibrant: swatchToOklch(vibrant),
    darkVibrant: swatchToOklch(darkVibrant),
    lightVibrant: swatchToOklch(lightVibrant),
    muted: swatchToOklch(muted),
    darkMuted: swatchToOklch(darkMuted),
    lightMuted: swatchToOklch(lightMuted),
    baseHue: Math.round(baseHcl.h),
    chroma: round(chroma, 4),
    source: "node-vibrant",
    generatedAt: new Date().toISOString(),
  };
}

function deriveWarehouseTokens(palette, mode = colorMode) {
  return mode === "light"
    ? deriveLightWarehouseTokens(palette)
    : deriveDarkWarehouseTokens(palette);
}

function deriveDarkWarehouseTokens(palette) {
  // DataRabbit's dark palette logic, narrowed to the tokens the Warehouse
  // shells and native chart surfaces consume.
  const main = palette.vibrant;
  const rvbbit = palette.lightVibrant;
  const seriesChroma = clamp(palette.chroma * 6, .12, .20);
  const background = oklch(13, .035, palette.baseHue);
  const secondaryBackground = oklch(18, .04, palette.baseHue);
  const foreground = oklch(92, 0, 0);
  const border = oklch(40, .03, palette.baseHue);
  const chromeBg = oklch(11, .034, palette.baseHue);
  const chromeBorder = oklch(30, .03, palette.baseHue);
  const chromeText = oklch(70, 0, 0);
  const docBg = oklch(12, .035, palette.baseHue);
  const blockBg = oklch(17, .035, palette.baseHue);
  const blockBgHover = oklch(20, .04, palette.baseHue);
  const blockBorder = oklch(32, .03, palette.baseHue);
  const chart1 = main;
  const chart2 = oklch(72, seriesChroma * .9, (palette.baseHue + 90) % 360);
  const chart3 = oklch(80, seriesChroma * .8, (palette.baseHue + 180) % 360);
  const chart4 = oklch(70, seriesChroma, (palette.baseHue + 240) % 360);
  const chart5 = oklch(72, seriesChroma * .85, (palette.baseHue + 320) % 360);
  const chart6 = oklch(74, seriesChroma * .95, (palette.baseHue + 290) % 360);

  return {
    "--background": background,
    "--secondary-background": secondaryBackground,
    "--foreground": foreground,
    "--border": border,
    "--ring": main,
    "--overlay": oklch(0, 0, 0, .7),
    "--main": main,
    "--main-foreground": oklch(0, 0, 0),
    "--chart-1": chart1,
    "--chart-2": chart2,
    "--chart-3": chart3,
    "--chart-4": chart4,
    "--chart-5": chart5,
    "--chart-6": chart6,
    "--success": "oklch(72% 0.19 145)",
    "--warning": "oklch(80% 0.16 85)",
    "--danger": "oklch(65% 0.22 25)",
    "--info": "oklch(70% 0.14 240)",
    "--chrome-bg": chromeBg,
    "--chrome-border": chromeBorder,
    "--chrome-text": chromeText,
    "--doc-bg": docBg,
    "--block-bg": blockBg,
    "--block-bg-hover": blockBgHover,
    "--block-border": blockBorder,
    "--grid-dot": oklch(22, .03, palette.baseHue),
    "--grid-outline": oklch(22, .03, palette.baseHue),
    "--rvbbit-accent": rvbbit,
    "--rvbbit-bg": oklch(17, .025, hueOf(rvbbit) ?? palette.baseHue),
    "--wallpaper-overlay-from": oklch(4, .02, palette.baseHue, .78),
    "--wallpaper-overlay-to": oklch(14, .06, palette.baseHue, .45),
    "--ambient-1": oklch(72, .16, palette.baseHue, .08),
    "--ambient-2": oklch(80, .16, (palette.baseHue + 120) % 360, .05),
    "--ambient-3": oklch(70, .18, (palette.baseHue + 240) % 360, .05),
    "--void": background,
    "--panel": blockBg,
    "--panel-raised": blockBgHover,
    "--panel-soft": "color-mix(in oklch, var(--panel) 74%, transparent)",
    "--bone": foreground,
    "--bone-bright": oklch(98, .006, palette.baseHue),
    "--fog": "color-mix(in oklch, var(--bone) 60%, transparent)",
    "--dim": "color-mix(in oklch, var(--bone) 34%, transparent)",
    "--faint": "color-mix(in oklch, var(--bone) 16%, transparent)",
    "--line": "color-mix(in oklch, var(--bone) 13%, transparent)",
    "--line-hot": "color-mix(in oklch, var(--main) 48%, transparent)",
    "--amber": main,
    "--amber-soft": "color-mix(in oklch, var(--main) 12%, transparent)",
    "--jade": rvbbit,
    "--jade-soft": "color-mix(in oklch, var(--rvbbit-accent) 10%, transparent)",
  };
}

function deriveLightWarehouseTokens(palette) {
  // Keep the selected room's identity in accents and visualization colors,
  // while moving structural surfaces to DataRabbit's warm-paper light branch.
  const main = palette.darkVibrant;
  const rvbbit = palette.darkVibrant;
  const seriesChroma = clamp(palette.chroma * 7, .13, .18);
  const background = oklch(96, .008, 95);
  const secondaryBackground = oklch(92, .012, 95);
  const foreground = oklch(21, .015, 260);
  const border = oklch(45, .02, 255);
  const chromeBg = oklch(91, .01, 95);
  const chromeBorder = oklch(73, .015, 250);
  const chromeText = oklch(39, .012, 255);
  const docBg = oklch(98, .003, 95);
  const blockBg = oklch(99, .004, 95);
  const blockBgHover = oklch(94, .012, hueOf(main) ?? palette.baseHue);
  const blockBorder = oklch(76, .016, 250);
  const chart1 = main;
  const chart2 = oklch(58, seriesChroma * .92, (palette.baseHue + 90) % 360);
  const chart3 = oklch(66, seriesChroma * .82, (palette.baseHue + 180) % 360);
  const chart4 = oklch(60, seriesChroma, (palette.baseHue + 240) % 360);
  const chart5 = oklch(60, seriesChroma * .92, (palette.baseHue + 320) % 360);
  const chart6 = oklch(64, seriesChroma * .96, (palette.baseHue + 290) % 360);

  return {
    "--background": background,
    "--secondary-background": secondaryBackground,
    "--foreground": foreground,
    "--border": border,
    "--ring": main,
    "--overlay": oklch(18, .01, 260, .14),
    "--main": main,
    "--main-foreground": oklch(99, 0, 0),
    "--chart-1": chart1,
    "--chart-2": chart2,
    "--chart-3": chart3,
    "--chart-4": chart4,
    "--chart-5": chart5,
    "--chart-6": chart6,
    "--success": "oklch(58% 0.17 145)",
    "--warning": "oklch(71% 0.16 85)",
    "--danger": "oklch(58% 0.21 25)",
    "--info": "oklch(58% 0.14 240)",
    "--chrome-bg": chromeBg,
    "--chrome-border": chromeBorder,
    "--chrome-text": chromeText,
    "--doc-bg": docBg,
    "--block-bg": blockBg,
    "--block-bg-hover": blockBgHover,
    "--block-border": blockBorder,
    "--grid-dot": oklch(85, .015, 240),
    "--grid-outline": oklch(82, .015, 245),
    "--rvbbit-accent": rvbbit,
    "--rvbbit-bg": oklch(94, .03, hueOf(rvbbit) ?? palette.baseHue),
    "--wallpaper-overlay-from": oklch(94, .01, palette.baseHue, .62),
    "--wallpaper-overlay-to": oklch(86, .03, palette.baseHue, .35),
    "--ambient-1": oklch(76, .10, palette.baseHue, .18),
    "--ambient-2": oklch(82, .12, (palette.baseHue + 120) % 360, .14),
    "--ambient-3": oklch(72, .14, (palette.baseHue + 240) % 360, .10),
    "--void": background,
    "--panel": blockBg,
    "--panel-raised": blockBgHover,
    "--panel-soft": "color-mix(in oklch, var(--panel) 80%, transparent)",
    "--bone": foreground,
    "--bone-bright": oklch(15, .012, 260),
    "--fog": "color-mix(in oklch, var(--bone) 68%, transparent)",
    "--dim": "color-mix(in oklch, var(--bone) 46%, transparent)",
    "--faint": "color-mix(in oklch, var(--bone) 16%, transparent)",
    "--line": "color-mix(in oklch, var(--bone) 15%, transparent)",
    "--line-hot": "color-mix(in oklch, var(--main) 52%, transparent)",
    "--amber": main,
    "--amber-soft": "color-mix(in oklch, var(--main) 11%, transparent)",
    "--jade": rvbbit,
    "--jade-soft": "color-mix(in oklch, var(--rvbbit-accent) 9%, transparent)",
  };
}

function swatchToOklch(swatch) {
  const { l, c, h } = rgbToOklch(...swatch.rgb);
  return `oklch(${round(l * 100, 2)}% ${round(c, 4)} ${round(h, 1)})`;
}

function pickMostPopulous(swatches) {
  let best = null;
  for (const swatch of swatches) {
    if (swatch && (!best || swatch.population > best.population)) best = swatch;
  }
  return best;
}

function firstNonNull(...candidates) {
  return candidates.find(Boolean) || null;
}

function darken(swatch) {
  const [r, g, b] = swatch.rgb;
  return cloneSwatch(swatch, [Math.round(r * .6), Math.round(g * .6), Math.round(b * .6)]);
}

function lighten(swatch) {
  const [r, g, b] = swatch.rgb;
  const mix = .45;
  return cloneSwatch(swatch, [
    Math.round(r + (255 - r) * mix),
    Math.round(g + (255 - g) * mix),
    Math.round(b + (255 - b) * mix),
  ]);
}

function cloneSwatch(template, rgb) {
  return new template.constructor(rgb, 1);
}

function rgbToOklch(r, g, b) {
  const lr = srgbToLin(r / 255);
  const lg = srgbToLin(g / 255);
  const lb = srgbToLin(b / 255);
  const l_ = .4122214708 * lr + .5363325363 * lg + .0514459929 * lb;
  const m_ = .2119034982 * lr + .6806995451 * lg + .1073969566 * lb;
  const s_ = .0883024619 * lr + .2817188376 * lg + .6299787005 * lb;
  const lc = Math.cbrt(l_);
  const mc = Math.cbrt(m_);
  const sc = Math.cbrt(s_);
  const l = .2104542553 * lc + .793617785 * mc - .0040720468 * sc;
  const a = 1.9779984951 * lc - 2.428592205 * mc + .4505937099 * sc;
  const bb = .0259040371 * lc + .7827717662 * mc - .808675766 * sc;
  return {
    l: clamp(l, 0, 1),
    c: Math.hypot(a, bb),
    h: ((Math.atan2(bb, a) * 180) / Math.PI + 360) % 360,
  };
}

function srgbToLin(value) {
  return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
}

function oklch(lightness, chroma, hue, alpha) {
  return alpha !== undefined && alpha !== 1
    ? `oklch(${lightness}% ${chroma} ${hue} / ${alpha})`
    : `oklch(${lightness}% ${chroma} ${hue})`;
}

function hueOf(value) {
  const match = String(value).match(/oklch\(\s*[\d.]+%?\s+[\d.]+\s+([\d.]+)/i);
  return match ? Number(match[1]) : null;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function round(value, digits) {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[character]);
}

function openDb() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("Browser image storage is unavailable."));
      return;
    }
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(DB_STORE)) {
        db.createObjectStore(DB_STORE, { keyPath: "id" });
      }
    };
    request.onerror = () => reject(request.error || new Error("Could not open browser image storage."));
    request.onsuccess = () => resolve(request.result);
  });
}

function withStore(mode, run) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const transaction = db.transaction(DB_STORE, mode);
    const store = transaction.objectStore(DB_STORE);
    const request = run(store);
    request.onerror = () => reject(request.error || new Error("Browser image storage failed."));
    request.onsuccess = () => resolve(request.result);
    transaction.oncomplete = () => db.close();
    transaction.onerror = () => {
      db.close();
      reject(transaction.error || new Error("Browser image storage failed."));
    };
    transaction.onabort = transaction.onerror;
  }));
}

function saveUploadBlob(file) {
  return withStore("readwrite", (store) => store.put({
    id: DB_KEY,
    blob: file,
    type: file.type,
    name: file.name,
    updatedAt: new Date().toISOString(),
  }));
}

async function loadUploadBlob() {
  const record = await withStore("readonly", (store) => store.get(DB_KEY));
  return record?.blob || null;
}

function clearUploadBlob() {
  return withStore("readwrite", (store) => store.delete(DB_KEY));
}
