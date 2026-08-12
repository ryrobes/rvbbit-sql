import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Excalidraw,
  exportToBlob,
} from "@excalidraw/excalidraw";
import type {
  AppState,
  BinaryFiles,
  ExcalidrawImperativeAPI,
} from "@excalidraw/excalidraw/types";
import type { ExcalidrawElement } from "@excalidraw/excalidraw/element/types";
import "@excalidraw/excalidraw/index.css";
import "./sketch.css";
import { materializeSketchElements } from "./scene";
import type { SketchScene } from "./scene";
import { applyAdaptiveSketchTheme } from "./theme";
import type { ThemeMode } from "./theme";

type SketchRecord = {
  id: string;
  title: string;
  revision: number;
  element_count: number;
  last_actor: "calliope" | "human" | "undo";
  last_operation_count: number;
  last_change_summary?: { element_ids?: string[] };
  can_undo_calliope?: boolean;
  scene: SketchScene;
};
type SketchResponse = { sketch: SketchRecord; read_only?: boolean; presentation?: boolean };

const mount = document.getElementById("sketch-root");
if (!mount) throw new Error("Sketch mount is unavailable");
document.documentElement.dataset.embedded = String(window.parent !== window);
const sketchId = String(mount.dataset.sketchId || "");
const declaredReadOnly = mount.dataset.readOnly === "true";
const declaredPresentation = mount.dataset.presentation === "true";
const sourceUrl = String(
  mount.dataset.sourceUrl || `/api/calliope/sketches/${encodeURIComponent(sketchId)}`,
);
const requestedTheme = new URLSearchParams(window.location.search).get("theme");

function parentMessage(type: string, detail: Record<string, unknown> = {}) {
  if (window.parent === window) return;
  window.parent.postMessage({ type, sketch_id: sketchId, ...detail }, window.location.origin);
}

async function requestJson<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  let body: any = null;
  try { body = await response.json(); } catch { /* handled below */ }
  if (!response.ok) {
    const error = new Error(body?.error?.message || `Sketch request failed (${response.status})`);
    (error as Error & { code?: string; status?: number }).code = body?.error?.code;
    (error as Error & { code?: string; status?: number }).status = response.status;
    throw error;
  }
  return body as T;
}

function sceneFingerprint(elements: readonly ExcalidrawElement[]) {
  return JSON.stringify(elements.filter((item) => !item.isDeleted).map((item) => {
    const { updated: _updated, versionNonce: _nonce, seed: _seed, index: _index, ...stable } = item;
    return stable;
  }));
}

function retainedAppState(appState: AppState | Record<string, unknown> | undefined) {
  const source = (appState || {}) as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const key of ["gridSize", "gridStep", "gridModeEnabled"]) {
    if (source[key] !== undefined) result[key] = source[key];
  }
  return result;
}

function backgroundFor(_theme: ThemeMode) {
  // Let the adaptive glass layer behind Excalidraw remain visible. The dark
  // theme still filters the drawing canvases, so existing element contrast is
  // preserved without baking a flat rectangle over the shared workspace.
  return "transparent";
}

function exportBackgroundFor(_theme: ThemeMode) {
  // Glass cannot survive a standalone PNG. Retain Excalidraw's light source
  // color here: exportWithDarkMode inverts it into the expected dark preview.
  return "#f5f8f5";
}

function blobDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")), { once: true });
    reader.addEventListener("error", () => reject(reader.error || new Error("Could not render preview")), { once: true });
    reader.readAsDataURL(blob);
  });
}

function SketchApp({ initial }: { initial: SketchResponse }) {
  const [sketch, setSketch] = useState(initial.sketch);
  const [readOnly] = useState(Boolean(initial.read_only || declaredReadOnly));
  const [presentation] = useState(Boolean(initial.presentation || declaredPresentation));
  const [theme, setTheme] = useState<ThemeMode>(() =>
    requestedTheme === "light" || requestedTheme === "dark"
      ? requestedTheme
      : window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark",
  );
  const [status, setStatus] = useState<"ready" | "dirty" | "saving" | "error">("ready");
  const [message, setMessage] = useState("");
  const [mounted, setMounted] = useState(false);
  const apiRef = useRef<ExcalidrawImperativeAPI | null>(null);
  const revisionRef = useRef(Number(initial.sketch.revision));
  const fingerprintRef = useRef("");
  const dirtyRef = useRef(false);
  const applyingRef = useRef(true);
  const savingRef = useRef(false);
  const saveTimerRef = useRef<number | null>(null);
  const previewTimerRef = useRef<number | null>(null);
  const forcePreviewRef = useRef<() => void>(() => {});

  const initialElements = useMemo(() => materializeSketchElements(initial.sketch.scene), [initial.sketch]);
  const initialData = useMemo(() => ({
    elements: initialElements,
    appState: {
      ...(initial.sketch.scene?.appState || {}),
      // Zen is a per-open presentation default, not shared scene state. Users
      // can still toggle it off without a later Calliope revision forcing it
      // back on during the same editing session.
      zenModeEnabled: true,
      viewBackgroundColor: backgroundFor(theme),
    },
    files: {},
    scrollToContent: true,
  }), [initialElements]); // Theme changes are applied imperatively after mount.

  const notifySaved = useCallback((next: SketchRecord, changed: boolean) => {
    parentMessage("calliope.sketch.saved", {
      revision: next.revision,
      changed,
      last_actor: next.last_actor,
      last_operation_count: next.last_operation_count,
      element_count: next.element_count,
      last_change_summary: next.last_change_summary || {},
      can_undo_calliope: Boolean(next.can_undo_calliope),
      title: next.title,
    });
  }, []);

  const applyRemote = useCallback((next: SketchRecord, highlight = true) => {
    const api = apiRef.current;
    if (!api) return;
    applyingRef.current = true;
    const elements = materializeSketchElements(next.scene);
    const changedIds = highlight && next.last_actor === "calliope"
      ? (next.last_change_summary?.element_ids || []).filter(Boolean)
      : [];
    const selectedElementIds = Object.fromEntries(
      changedIds.map((id) => [id, true as const]),
    ) as Record<string, true>;
    api.updateScene({
      elements,
      appState: {
        ...(next.scene?.appState || {}),
        viewBackgroundColor: backgroundFor(theme),
        selectedElementIds,
      },
    });
    api.history.clear();
    api.scrollToContent(elements, { fitToContent: true, animate: true });
    revisionRef.current = Number(next.revision);
    fingerprintRef.current = sceneFingerprint(elements);
    dirtyRef.current = false;
    setSketch(next);
    setStatus("ready");
    setMessage("");
    window.setTimeout(() => { applyingRef.current = false; }, 0);
    if (!readOnly) {
      if (previewTimerRef.current != null) window.clearTimeout(previewTimerRef.current);
      previewTimerRef.current = window.setTimeout(() => {
        previewTimerRef.current = null;
        forcePreviewRef.current();
      }, 350);
    }
    if (changedIds.length) {
      window.setTimeout(() => {
        if (revisionRef.current !== Number(next.revision) || !apiRef.current) return;
        apiRef.current.updateScene({ appState: { selectedElementIds: {} } });
      }, 1800);
    }
  }, [readOnly, theme]);

  const saveNow = useCallback(async (forcePreview = false) => {
    const api = apiRef.current;
    if (!api || readOnly || savingRef.current) return;
    const elements = api.getSceneElements();
    const fingerprint = sceneFingerprint(elements);
    if (!forcePreview && fingerprint === fingerprintRef.current) return;
    savingRef.current = true;
    setStatus("saving");
    setMessage("Saving");
    try {
      const appState = api.getAppState();
      const blob = await exportToBlob({
        elements: elements.filter((item) => !item.isDeleted),
        appState: {
          ...appState,
          viewBackgroundColor: exportBackgroundFor(theme),
          exportBackground: true,
          exportWithDarkMode: theme === "dark",
        },
        files: api.getFiles() as BinaryFiles,
        mimeType: "image/png",
        maxWidthOrHeight: 1600,
        exportPadding: 42,
      });
      const result = await requestJson<{ changed: boolean; sketch: SketchRecord }>(
        `/api/calliope/sketches/${encodeURIComponent(sketchId)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            expected_revision: revisionRef.current,
            elements,
            app_state: retainedAppState(appState),
            preview_data_url: await blobDataUrl(blob),
            preview_only: forcePreview,
          }),
        },
      );
      revisionRef.current = Number(result.sketch.revision);
      fingerprintRef.current = fingerprint;
      dirtyRef.current = false;
      setSketch(result.sketch);
      setStatus("ready");
      setMessage(result.changed ? `Saved · r${result.sketch.revision}` : "");
      notifySaved(result.sketch, result.changed);
    } catch (error: any) {
      if (error?.status === 409) {
        try {
          const latest = await requestJson<SketchResponse>(sourceUrl);
          applyRemote(latest.sketch, true);
          setMessage("Reloaded newer edit");
          parentMessage("calliope.sketch.conflict", { revision: latest.sketch.revision });
          return;
        } catch { /* surface the original conflict below */ }
      }
      setStatus("error");
      setMessage(error?.message || "Could not save");
      parentMessage("calliope.sketch.error", { message: error?.message || "Could not save" });
    } finally {
      savingRef.current = false;
    }
  }, [applyRemote, notifySaved, readOnly, theme]);

  forcePreviewRef.current = () => void saveNow(true);

  const scheduleSave = useCallback(() => {
    if (readOnly) return;
    dirtyRef.current = true;
    setStatus("dirty");
    setMessage("Unsaved edit");
    parentMessage("calliope.sketch.dirty", { revision: revisionRef.current });
    if (saveTimerRef.current != null) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => void saveNow(false), 1000);
  }, [readOnly, saveNow]);

  const onChange = useCallback((
    elements: readonly ExcalidrawElement[],
    appState: AppState,
  ) => {
    const sidebar = appState.openSidebar;
    if (sidebar?.name === "default" && (!sidebar.tab || sidebar.tab === "library")) {
      apiRef.current?.updateScene({ appState: { openSidebar: null } });
    }
    if (!mounted || applyingRef.current || readOnly) return;
    if (sceneFingerprint(elements) !== fingerprintRef.current) scheduleSave();
  }, [mounted, readOnly, scheduleSave]);

  const setApi = useCallback((api: ExcalidrawImperativeAPI) => {
    apiRef.current = api;
    fingerprintRef.current = sceneFingerprint(api.getSceneElements());
    applyingRef.current = false;
    setMounted(true);
    window.requestAnimationFrame(() => {
      api.scrollToContent(api.getSceneElements(), { fitToContent: true, animate: false });
      parentMessage("calliope.sketch.ready", {
        revision: revisionRef.current,
        read_only: readOnly,
      });
      parentMessage("calliope.sketch.theme.request");
      if (!readOnly) {
        previewTimerRef.current = window.setTimeout(() => void saveNow(true), 450);
      }
    });
  }, [readOnly, saveNow]);

  const undoCalliope = useCallback(async () => {
    if (readOnly || savingRef.current || dirtyRef.current) return;
    savingRef.current = true;
    setStatus("saving");
    setMessage("Undoing Calliope");
    try {
      const result = await requestJson<{ sketch: SketchRecord }>(
        `/api/calliope/sketches/${encodeURIComponent(sketchId)}/undo-calliope`,
        {
          method: "POST",
          body: JSON.stringify({ expected_revision: revisionRef.current }),
        },
      );
      applyRemote(result.sketch, false);
      notifySaved(result.sketch, true);
    } catch (error: any) {
      setStatus("error");
      setMessage(error?.message || "Could not undo");
    } finally {
      savingRef.current = false;
    }
  }, [applyRemote, notifySaved, readOnly]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    // Excalidraw finishes applying initialData during its own commit. Reapply
    // the viewer color one task later so a late parent-theme handoff wins that
    // race instead of leaving dark controls over a light canvas (or vice versa).
    const timer = window.setTimeout(() => {
      apiRef.current?.updateScene({
        appState: { viewBackgroundColor: backgroundFor(theme) },
      });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [mounted, theme]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== window.parent) return;
      if (event.data?.type === "calliope.sketch.theme.apply") {
        setTheme(applyAdaptiveSketchTheme(event.data));
      } else if (event.data?.type === "calliope.sketch.flush") {
        if (saveTimerRef.current != null) window.clearTimeout(saveTimerRef.current);
        void saveNow(false);
      } else if (event.data?.type === "calliope.sketch.viewport.changed") {
        window.requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [saveNow]);

  useEffect(() => {
    if (presentation) return;
    const interval = window.setInterval(async () => {
      if (document.hidden || dirtyRef.current || savingRef.current) return;
      try {
        const latest = await requestJson<SketchResponse>(sourceUrl);
        if (Number(latest.sketch.revision) > revisionRef.current) {
          applyRemote(latest.sketch, true);
        }
      } catch { /* the visible editor retains its last safe checkpoint */ }
    }, 2500);
    return () => window.clearInterval(interval);
  }, [applyRemote, presentation]);

  useEffect(() => () => {
    if (saveTimerRef.current != null) window.clearTimeout(saveTimerRef.current);
    if (previewTimerRef.current != null) window.clearTimeout(previewTimerRef.current);
  }, []);

  const topRight = useCallback(() => (
    <div className="sketch-tools" aria-live="polite">
      <span className={status === "error" ? "error" : status === "dirty" ? "dirty" : "muted"}>
        {readOnly ? "View only" : message || `Saved · r${sketch.revision}`}
      </span>
      {!readOnly && (
        <button
          type="button"
          disabled={!sketch.can_undo_calliope || status !== "ready"}
          onClick={() => void undoCalliope()}
          title="Undo Calliope's latest revision"
        >Undo Callie</button>
      )}
      <button
        type="button"
        onClick={() => parentMessage("calliope.sketch.expand.request")}
        title="Open the Sketch in a large workspace"
        aria-label="Open large workspace"
      >↗</button>
    </div>
  ), [message, readOnly, sketch.can_undo_calliope, sketch.revision, status, undoCalliope]);

  return (
    <main className={`sketch-app ${readOnly ? "read-only" : ""} ${presentation ? "presentation" : ""}`}>
      <div className="sketch-canvas">
        <Excalidraw
          initialData={initialData}
          excalidrawAPI={setApi}
          onChange={onChange}
          viewModeEnabled={readOnly}
          theme={theme}
          name={sketch.title}
          autoFocus={false}
          detectScroll={false}
          handleKeyboardGlobally={false}
          renderTopRightUI={presentation ? undefined : topRight}
          UIOptions={{
            canvasActions: {
              loadScene: false,
              saveToActiveFile: false,
              export: false,
              toggleTheme: false,
              saveAsImage: false,
            },
            tools: { image: false },
          }}
        />
      </div>
      <div className={`sketch-loading ${mounted ? "done" : ""}`} aria-hidden={mounted}>
        <i></i><strong>{presentation ? "Opening plan" : "Opening shared sketch"}</strong><span>{presentation ? "Restoring the pinned read-only revision" : "Restoring the latest editable checkpoint"}</span>
      </div>
    </main>
  );
}

async function boot() {
  if (!sketchId) throw new Error("Sketch identity is missing");
  const initial = await requestJson<SketchResponse>(sourceUrl);
  createRoot(mount!).render(<SketchApp initial={initial} />);
}

void boot().catch((error) => {
  mount.innerHTML = `<div class="sketch-fatal"><strong>Sketch unavailable</strong><span></span></div>`;
  const detail = mount.querySelector("span");
  if (detail) detail.textContent = error?.message || "The latest checkpoint could not be opened.";
  parentMessage("calliope.sketch.error", { message: error?.message || "Sketch unavailable" });
});
