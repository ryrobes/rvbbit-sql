(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const els = {
    sessionList: $("#session-list"),
    sessionSearch: $("#session-search"),
    newSession: $("#new-session"),
    dialog: $("#new-session-dialog"),
    newSessionForm: $("#new-session-form"),
    newSessionTitle: $("#new-session-title"),
    createSession: $("#create-session"),
    sessionTitle: $("#session-title"),
    archiveSession: $("#archive-session"),
    notebook: $(".notebook"),
    chatResizer: $("#chat-resizer"),
    stageScroll: $("#stage-scroll"),
    stage: $("#stage"),
    stageEmpty: $("#stage-empty"),
    surfaceCount: $("#surface-count"),
    newSurfaces: $("#new-surfaces"),
    messages: $("#messages"),
    chatEmpty: $("#chat-empty"),
    status: $("#calliope-status"),
    avatar: $("#calliope-avatar"),
    toolActivity: $("#tool-activity"),
    composer: $("#composer"),
    input: $("#message-input"),
    send: $("#send-message"),
    designProfileChip: $("#design-profile-chip"),
    imageInput: $("#image-input"),
    attachmentTray: $("#attachment-tray"),
    selectedReference: $("#selected-reference"),
    spatialSelectionTray: $("#spatial-selection-tray"),
    markupDialog: $("#markup-dialog"),
    markupTitle: $("#markup-title"),
    markupToolbar: $("#markup-toolbar"),
    markupCanvas: $("#markup-canvas"),
    markupLoading: $("#markup-loading"),
    markupClose: $("#markup-close"),
    markupCancel: $("#markup-cancel"),
    markupAttach: $("#markup-attach"),
    markupUndo: $("#markup-undo"),
    markupClear: $("#markup-clear"),
    mobileSessions: $("#mobile-sessions-toggle"),
    mobileChat: $("#mobile-chat-toggle"),
    mobileShade: $("#mobile-shade"),
    styleOpen: $("#style-library-open"),
    styleDialog: $("#style-library-dialog"),
    styleClose: $("#style-library-close"),
    styleNew: $("#style-new"),
    styleList: $("#style-list"),
    styleCreatePane: $("#style-create-pane"),
    styleEditorPane: $("#style-editor-pane"),
    styleName: $("#style-name"),
    styleUrl: $("#style-url"),
    styleGuidance: $("#style-guidance"),
    styleImages: $("#style-images"),
    styleUseSelected: $("#style-use-selected"),
    styleSourceStrip: $("#style-source-strip"),
    styleGenerate: $("#style-generate"),
    styleGenerateStatus: $("#style-generate-status"),
    styleEditorName: $("#style-editor-name"),
    styleEditorDescription: $("#style-editor-description"),
    styleOwner: $("#style-owner"),
    styleVersion: $("#style-version"),
    styleReferenceStrip: $("#style-reference-strip"),
    stylePreview: $("#style-preview"),
    styleSourceSummary: $("#style-source-summary"),
    styleMarkdown: $("#style-markdown"),
    styleArchive: $("#style-archive"),
    styleFork: $("#style-fork"),
    styleSaveVersion: $("#style-save-version"),
    styleUseOnce: $("#style-use-once"),
    styleUseSession: $("#style-use-session"),
    toast: $("#toast"),
  };

  const state = {
    sessions: [],
    current: null,
    turns: [],
    surfaces: [],
    selectedSurfaceId: null,
    spatialSelections: [],
    inspectingSurfaceId: null,
    attachments: [],
    busy: false,
    stageAtLiveEdge: true,
    newSurfaceCount: 0,
    config: null,
    artifactResizeTimer: null,
    avatarTimer: null,
    chatWidth: null,
    cubeBuilders: new Map(),
    designProfiles: [],
    designProfileId: null,
    designProfileVersionId: null,
    nextTurnDesignProfileVersionId: null,
    designSourceImages: [],
    useSelectedAsDesignSource: false,
    markup: {
      surface: null,
      image: null,
      strokes: [],
      liveStroke: null,
      pendingSelection: null,
      tool: "select",
      color: "#ff4d4f",
      width: 6,
      ready: false,
    },
  };

  const THINKING_STATES = ["working", "composing", "solving"];
  const CHAT_WIDTH_KEY = "rvbbit-calliope-chat-width-v1";
  const CHAT_MIN_WIDTH = 320;
  const CHAT_DEFAULT_WIDTH = 390;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function safeMarkdown(value) {
    let text = escapeHtml(value || "");
    const blocks = [];
    text = text.replace(/```([\s\S]*?)```/g, (_, code) => {
      const key = `@@BLOCK${blocks.length}@@`;
      blocks.push(`<pre><code>${code.trim()}</code></pre>`);
      return key;
    });
    text = text
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
      .replace(
        /\[([^\]]+)\]\(((?:https?:\/\/|\/api\/calliope\/files\/)[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>',
      );
    text = text
      .split(/\n{2,}/)
      .map((part) => part.startsWith("@@BLOCK") ? part : `<p>${part.replaceAll("\n", "<br>")}</p>`)
      .join("");
    blocks.forEach((block, index) => {
      text = text.replace(`@@BLOCK${index}@@`, block);
    });
    return text || "<p></p>";
  }

  function relativeTime(value) {
    if (!value) return "";
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    const units = [
      [31536000, "y"], [2592000, "mo"], [604800, "w"], [86400, "d"],
      [3600, "h"], [60, "m"],
    ];
    for (const [span, label] of units) {
      if (seconds >= span) return `${Math.floor(seconds / span)}${label} ago`;
    }
    return "now";
  }

  function toast(message, error = false) {
    els.toast.textContent = message;
    els.toast.classList.toggle("error", error);
    els.toast.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => els.toast.classList.remove("show"), 2800);
  }

  function chatWidthBounds() {
    const compact = window.innerWidth <= 1120;
    const rail = compact ? 205 : 238;
    const minimumStage = compact ? 390 : 430;
    const available = window.innerWidth - rail - minimumStage;
    return {
      min: CHAT_MIN_WIDTH,
      max: Math.max(
        CHAT_MIN_WIDTH,
        Math.min(720, Math.floor(window.innerWidth * .48), available),
      ),
    };
  }

  function setChatWidth(value, persist = true) {
    if (!els.notebook || !els.chatResizer) return;
    const bounds = chatWidthBounds();
    const width = Math.round(Math.min(bounds.max, Math.max(bounds.min, Number(value) || CHAT_DEFAULT_WIDTH)));
    state.chatWidth = width;
    els.notebook.style.setProperty("--calliope-chat-width", `${width}px`);
    els.chatResizer.setAttribute("aria-valuemin", String(bounds.min));
    els.chatResizer.setAttribute("aria-valuemax", String(bounds.max));
    els.chatResizer.setAttribute("aria-valuenow", String(width));
    els.chatResizer.title = `Conversation width · ${width}px`;
    if (persist) {
      try { localStorage.setItem(CHAT_WIDTH_KEY, String(width)); } catch {}
    }
  }

  function restoreChatWidth() {
    let saved = CHAT_DEFAULT_WIDTH;
    try { saved = Number(localStorage.getItem(CHAT_WIDTH_KEY)) || saved; } catch {}
    setChatWidth(saved, false);
  }

  function beginChatResize(event) {
    if (window.matchMedia("(max-width: 880px)").matches || event.button !== 0) return;
    event.preventDefault();
    els.chatResizer.setPointerCapture(event.pointerId);
    els.chatResizer.classList.add("dragging");
    document.body.classList.add("chat-resizing");
  }

  function moveChatResize(event) {
    if (!els.chatResizer.hasPointerCapture(event.pointerId)) return;
    setChatWidth(window.innerWidth - event.clientX, false);
  }

  function endChatResize(event) {
    if (!els.chatResizer.hasPointerCapture(event.pointerId)) return;
    els.chatResizer.releasePointerCapture(event.pointerId);
    els.chatResizer.classList.remove("dragging");
    document.body.classList.remove("chat-resizing");
    setChatWidth(state.chatWidth, true);
    resetArtifactFrameHeights();
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    let data = {};
    const text = await response.text();
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { error: { message: text } };
    }
    if (!response.ok) {
      throw new Error(data?.error?.message || data?.error?.code || `Request failed (${response.status})`);
    }
    return data;
  }

  function setStatus(label, mode = "") {
    els.status.className = `agent-status ${mode}`;
    $("span", els.status).textContent = label;
  }

  function updateCalliopeAvatar(now = new Date()) {
    if (!els.avatar) return;
    const hour = now.getHours();
    const period = hour >= 7 && hour < 19 ? "day" : "night";
    const frame = els.avatar.closest(".muse-avatar");
    const src = period === "day" ? frame?.dataset.daySrc : frame?.dataset.nightSrc;
    if (src && els.avatar.getAttribute("src") !== src) els.avatar.src = src;
    if (frame) frame.dataset.period = period;
  }

  function scheduleAvatarClock() {
    updateCalliopeAvatar();
    clearTimeout(state.avatarTimer);
    const nextMinute = 60_050 - (Date.now() % 60_000);
    state.avatarTimer = setTimeout(scheduleAvatarClock, nextMinute);
  }

  function setMobilePanel(panel = null) {
    const smallScreen = window.matchMedia("(max-width: 880px)").matches;
    const sessionsOpen = smallScreen && panel === "sessions";
    const chatOpen = smallScreen && panel === "chat";
    document.body.classList.toggle("mobile-sessions-open", sessionsOpen);
    document.body.classList.toggle("mobile-chat-open", chatOpen);
    els.mobileSessions.setAttribute("aria-expanded", String(sessionsOpen));
    els.mobileChat.setAttribute("aria-expanded", String(chatOpen));
  }

  async function loadConfig() {
    try {
      state.config = await api("/api/calliope/config");
      setStatus(state.config.healthy ? "ready" : "unavailable", state.config.healthy ? "" : "offline");
    } catch (error) {
      setStatus("unavailable", "offline");
      throw error;
    }
  }

  function designVersions(profile) {
    if (Array.isArray(profile?.versions) && profile.versions.length) return profile.versions;
    return profile?.version ? [profile.version] : [];
  }

  function designVersionById(versionId) {
    if (!versionId) return null;
    for (const profile of state.designProfiles) {
      const version = designVersions(profile).find((item) => item.id === versionId);
      if (version) return { profile, version };
    }
    return null;
  }

  function selectedSurfaceDesignVersionId() {
    const surface = state.surfaces.find((item) => item.id === state.selectedSurfaceId);
    return surface?.presentation?.design_profile?.version_id
      || surface?.design_profile_version_id
      || null;
  }

  function effectiveComposerDesignProfile() {
    const choices = [
      [state.nextTurnDesignProfileVersionId, "next turn"],
      [selectedSurfaceDesignVersionId(), "selected artifact"],
      [state.current?.design_profile_version_id, "session"],
    ];
    for (const [versionId, mode] of choices) {
      if (!versionId) continue;
      const found = designVersionById(versionId);
      if (found) return { ...found, mode };
      const surface = state.surfaces.find((item) =>
        item.presentation?.design_profile?.version_id === versionId
      );
      const snapshot = surface?.presentation?.design_profile;
      if (snapshot) {
        return {
          mode,
          profile: { id: snapshot.profile_id, name: snapshot.name },
          version: { id: snapshot.version_id, version: snapshot.version },
        };
      }
    }
    return null;
  }

  function renderDesignProfileChip() {
    const active = effectiveComposerDesignProfile();
    els.designProfileChip.hidden = !active;
    if (!active) {
      els.designProfileChip.innerHTML = "";
      return;
    }
    const clearMode = active.mode === "selected artifact"
      ? "surface"
      : active.mode === "next turn" ? "once" : "session";
    els.designProfileChip.innerHTML = `<i aria-hidden="true"></i>
      <span>Design · ${escapeHtml(active.mode)}</span>
      <strong data-open-design-profile="${escapeHtml(active.profile.id || "")}">${
        escapeHtml(active.profile.name || "Pinned profile")
      } · v${escapeHtml(active.version.version || "?")}</strong>
      <button type="button" data-clear-design-profile="${clearMode}" aria-label="Clear Design Profile">×</button>`;
  }

  function mergeDesignProfile(profile) {
    const index = state.designProfiles.findIndex((item) => item.id === profile.id);
    if (index >= 0) state.designProfiles[index] = profile;
    else state.designProfiles.unshift(profile);
  }

  async function loadDesignProfiles() {
    const data = await api("/api/calliope/styles");
    state.designProfiles = data.profiles || [];
    renderDesignProfileList();
    renderDesignProfileChip();
  }

  function renderDesignProfileList() {
    const visibleProfiles = state.designProfiles.filter((profile) => !profile.archived);
    if (!visibleProfiles.length) {
      els.styleList.innerHTML = '<div class="style-list-empty">No Design Profiles yet.<br>Create the company’s first reusable visual language.</div>';
      return;
    }
    els.styleList.innerHTML = visibleProfiles.map((profile) => `
      <button class="style-list-card ${state.designProfileId === profile.id ? "active" : ""}"
              type="button" data-design-profile="${escapeHtml(profile.id)}">
        <i aria-hidden="true"></i>
        <strong>${escapeHtml(profile.name)}</strong>
        <span>v${escapeHtml(profile.current_version)} · ${escapeHtml(profile.can_edit ? "yours" : profile.owner_email)}</span>
      </button>`).join("");
  }

  function resetDesignSourceForm() {
    state.designSourceImages = [];
    state.useSelectedAsDesignSource = false;
    els.styleName.value = "";
    els.styleUrl.value = "";
    els.styleGuidance.value = "";
    els.styleImages.value = "";
    els.styleGenerateStatus.textContent = "";
    renderDesignSourceStrip();
  }

  function eligibleSelectedDesignSource() {
    return state.surfaces.find((item) =>
      item.id === state.selectedSurfaceId && ["image", "artifact"].includes(item.kind)
    ) || null;
  }

  function syncSelectedDesignSourceButton() {
    const surface = eligibleSelectedDesignSource();
    els.styleUseSelected.disabled = !surface;
    els.styleUseSelected.classList.toggle("active", Boolean(surface && state.useSelectedAsDesignSource));
    els.styleUseSelected.innerHTML = surface
      ? `<span>⌖</span> ${state.useSelectedAsDesignSource ? "Using" : "Use"} ${escapeHtml(surface.title)}`
      : "<span>⌖</span> Select a capture or artifact first";
  }

  function renderDesignSourceStrip() {
    const sourceCards = state.designSourceImages.map((item, index) => `
      <div class="style-source-thumb">
        <img src="${escapeHtml(item.data_url)}" alt="${escapeHtml(item.name)}">
        <span>${escapeHtml(item.name)}</span>
        <button type="button" data-remove-design-source="${index}" aria-label="Remove ${escapeHtml(item.name)}">×</button>
      </div>`).join("");
    const selected = state.useSelectedAsDesignSource ? eligibleSelectedDesignSource() : null;
    const selectedCard = selected
      ? `<div class="style-source-thumb"><span>Selected · ${escapeHtml(selected.title)}</span></div>`
      : "";
    els.styleSourceStrip.innerHTML = sourceCards + selectedCard;
    els.styleSourceStrip.hidden = !sourceCards && !selectedCard;
    syncSelectedDesignSourceButton();
  }

  function showNewDesignProfile() {
    state.designProfileId = null;
    state.designProfileVersionId = null;
    els.styleCreatePane.hidden = false;
    els.styleEditorPane.hidden = true;
    resetDesignSourceForm();
    renderDesignProfileList();
    requestAnimationFrame(() => els.styleName.focus());
  }

  async function openDesignProfiles(profileId = null) {
    const opening = !els.styleDialog.open;
    if (opening) {
      els.styleDialog.showModal();
      await loadDesignProfiles();
    }
    syncSelectedDesignSourceButton();
    const target = profileId || state.designProfileId || state.designProfiles[0]?.id;
    if (target) await selectDesignProfile(target);
    else showNewDesignProfile();
  }

  function selectedDesignVersion() {
    const profile = state.designProfiles.find((item) => item.id === state.designProfileId);
    if (!profile) return null;
    const versions = designVersions(profile);
    const version = versions.find((item) => item.id === state.designProfileVersionId)
      || versions.find((item) => Number(item.version) === Number(profile.current_version))
      || versions[0];
    return version ? { profile, version } : null;
  }

  function safeStyleColor(value, fallback) {
    const candidate = String(value || "").trim();
    return candidate && globalThis.CSS?.supports?.("color", candidate) ? candidate : fallback;
  }

  function safeStyleLength(property, value, fallback) {
    const candidate = String(value || "").trim();
    return candidate && globalThis.CSS?.supports?.(property, candidate) ? candidate : fallback;
  }

  function safeStyleFont(value, fallback) {
    const candidate = String(value || "").trim();
    return candidate && !/[;{}<>]|url\s*\(/i.test(candidate) ? candidate : fallback;
  }

  function renderDesignPreview(profile, version) {
    const tokens = version.tokens || {};
    const palette = tokens.palette || {};
    const typography = tokens.typography || {};
    const shape = tokens.shape || {};
    const effects = tokens.effects || {};
    const chart = tokens.charts || {};
    const colors = {
      bg: safeStyleColor(palette.background, "#10151a"),
      surface: safeStyleColor(palette.surface, "#172027"),
      surfaceAlt: safeStyleColor(palette.surface_alt, "#121b21"),
      text: safeStyleColor(palette.text, "#f3f5f6"),
      muted: safeStyleColor(palette.muted, "#87929a"),
      accent: safeStyleColor(palette.accent, "#68c7b2"),
      accentAlt: safeStyleColor(palette.accent_alt, "#f5b446"),
      border: safeStyleColor(palette.border, "rgba(255,255,255,.12)"),
    };
    const series = Array.isArray(chart.series)
      ? chart.series.map((color) => safeStyleColor(color, colors.accent)).slice(0, 6)
      : [];
    const bars = [38, 66, 49, 84, 58, 96, 73].map((height, index) =>
      `<i style="--bar:${height}%;--bar-color:${escapeHtml(series[index % Math.max(1, series.length)] || (index % 3 === 1 ? colors.accentAlt : colors.accent))}"></i>`
    ).join("");
    els.stylePreview.innerHTML = `
      <div class="style-preview-top"><i></i><strong>${escapeHtml(profile.name)}</strong><span>Operations overview</span></div>
      <div class="style-preview-body">
        <div class="style-preview-kicker">Live signal · current period</div>
        <div class="style-preview-title">Clarity at decision speed.</div>
        <div class="style-preview-metrics">
          <div class="style-preview-metric"><span>Pipeline</span><b>$2.4m</b></div>
          <div class="style-preview-metric"><span>Conversion</span><b>18.7%</b></div>
          <div class="style-preview-metric"><span>At risk</span><b>14</b></div>
        </div>
        <div class="style-preview-chart">${bars}</div>
      </div>`;
    const variables = {
      "--sp-bg": colors.bg,
      "--sp-surface": colors.surface,
      "--sp-surface-alt": colors.surfaceAlt,
      "--sp-text": colors.text,
      "--sp-muted": colors.muted,
      "--sp-accent": colors.accent,
      "--sp-border": colors.border,
      "--sp-display": safeStyleFont(typography.display, "ui-sans-serif, sans-serif"),
      "--sp-body": safeStyleFont(typography.body, "ui-sans-serif, sans-serif"),
      "--sp-mono": safeStyleFont(typography.mono, "ui-monospace, monospace"),
      "--sp-radius": safeStyleLength("border-radius", shape.radius, "0px"),
      "--sp-shadow": safeStyleLength("box-shadow", effects.shadow, "0 18px 45px rgba(0,0,0,.3)"),
    };
    Object.entries(variables).forEach(([name, value]) => els.stylePreview.style.setProperty(name, value));
  }

  function renderDesignReferences(version) {
    const assets = version.assets || [];
    els.styleReferenceStrip.innerHTML = assets.map((asset) => {
      if (asset.url) {
        return `<div class="style-reference-card">
          <img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.original_name || "Design reference")}">
          <span>${escapeHtml(asset.source_kind)} · ${escapeHtml(asset.original_name || "reference")}</span>
        </div>`;
      }
      return `<div class="style-reference-card url-only">
        <b title="${escapeHtml(asset.source_url || "Frozen source")}">${escapeHtml(asset.source_url || asset.source_kind)}</b>
      </div>`;
    }).join("");
  }

  function renderDesignEditor() {
    const selected = selectedDesignVersion();
    if (!selected) {
      showNewDesignProfile();
      return;
    }
    const { profile, version } = selected;
    state.designProfileVersionId = version.id;
    els.styleCreatePane.hidden = true;
    els.styleEditorPane.hidden = false;
    els.styleEditorName.textContent = profile.name;
    els.styleEditorDescription.textContent = profile.description || "No description supplied.";
    els.styleOwner.textContent = profile.can_edit
      ? `Created by you · company visible`
      : `Created by ${profile.owner_email} · duplicate to revise`;
    els.styleVersion.innerHTML = designVersions(profile).map((item) =>
      `<option value="${escapeHtml(item.id)}" ${item.id === version.id ? "selected" : ""}>v${escapeHtml(item.version)}${
        Number(item.version) === Number(profile.current_version) ? " · current" : ""
      }</option>`
    ).join("");
    els.styleMarkdown.value = version.markdown || "";
    els.styleMarkdown.readOnly = !profile.can_edit;
    els.styleSaveVersion.disabled = !profile.can_edit;
    els.styleArchive.disabled = !profile.can_edit;
    els.styleUseOnce.disabled = !state.current || profile.archived;
    els.styleUseSession.disabled = !state.current || profile.archived;
    els.styleUseSession.textContent = state.current?.design_profile_version_id === version.id
      ? "Using in this session"
      : "Use in this session";
    els.styleUseOnce.textContent = state.nextTurnDesignProfileVersionId === version.id
      ? "Using next turn"
      : "Use next turn";
    els.styleSourceSummary.textContent = version.source_summary || "";
    renderDesignReferences(version);
    renderDesignPreview(profile, version);
    renderDesignProfileList();
  }

  async function selectDesignProfile(profileId) {
    const data = await api(`/api/calliope/styles/${encodeURIComponent(profileId)}`);
    mergeDesignProfile(data.profile);
    state.designProfileId = data.profile.id;
    state.designProfileVersionId = data.profile.version?.id || designVersions(data.profile)[0]?.id || null;
    renderDesignEditor();
    renderDesignProfileChip();
  }

  async function readDesignSourceImages(files) {
    const accepted = [...files].slice(0, Math.max(0, 4 - state.designSourceImages.length));
    for (const file of accepted) {
      if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) {
        toast(`${file.name} is not a supported image`, true);
        continue;
      }
      if (state.config?.max_image_bytes && file.size > state.config.max_image_bytes) {
        toast(`${file.name} is too large`, true);
        continue;
      }
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      state.designSourceImages.push({ name: file.name, data_url: dataUrl });
    }
    renderDesignSourceStrip();
  }

  async function generateDesignProfile() {
    const name = els.styleName.value.trim();
    if (!name) {
      toast("Give the Design Profile a name", true);
      els.styleName.focus();
      return;
    }
    const selected = state.useSelectedAsDesignSource ? eligibleSelectedDesignSource() : null;
    els.styleGenerate.disabled = true;
    els.styleGenerateStatus.textContent = "Calliope is reading the references and building the profile…";
    try {
      const data = await api("/api/calliope/styles", {
        method: "POST",
        body: JSON.stringify({
          name,
          source_url: els.styleUrl.value.trim(),
          guidance: els.styleGuidance.value.trim(),
          attachments: state.designSourceImages,
          selected_surface_id: selected?.id || null,
        }),
      });
      mergeDesignProfile(data.profile);
      state.designProfileId = data.profile.id;
      state.designProfileVersionId = data.profile.version.id;
      resetDesignSourceForm();
      renderDesignEditor();
      toast(`Design Profile created · ${data.profile.name}`);
    } finally {
      els.styleGenerate.disabled = false;
      els.styleGenerateStatus.textContent = "";
    }
  }

  async function saveDesignProfileVersion() {
    const selected = selectedDesignVersion();
    if (!selected?.profile.can_edit) return;
    const markdown = els.styleMarkdown.value.trim();
    const data = await api(
      `/api/calliope/styles/${encodeURIComponent(selected.profile.id)}/versions`,
      {
        method: "POST",
        body: JSON.stringify({
          markdown,
          tokens: selected.version.tokens || {},
          source_summary: selected.version.source_summary || "",
        }),
      },
    );
    mergeDesignProfile(data.profile);
    state.designProfileId = data.profile.id;
    state.designProfileVersionId = data.profile.version.id;
    renderDesignEditor();
    renderDesignProfileChip();
    toast(`Saved ${data.profile.name} · v${data.profile.current_version}`);
  }

  async function applyDesignProfileToSession(versionId) {
    if (!state.current) return;
    const data = await api(`/api/calliope/sessions/${encodeURIComponent(state.current.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ design_profile_version_id: versionId }),
    });
    state.current = data.session;
    const summary = state.sessions.find((item) => item.id === state.current.id);
    if (summary) summary.design_profile_version_id = versionId;
    renderDesignEditor();
    renderDesignProfileChip();
  }

  async function clearComposerDesignProfile(mode) {
    if (mode === "once") {
      state.nextTurnDesignProfileVersionId = null;
    } else if (mode === "surface") {
      clearSurfaceSelection();
    } else if (mode === "session" && state.current) {
      await applyDesignProfileToSession(null);
    }
    renderDesignProfileChip();
  }

  async function archiveDesignProfile() {
    const selected = selectedDesignVersion();
    if (!selected?.profile.can_edit) return;
    if (!window.confirm(`Archive “${selected.profile.name}”? Existing artifacts retain their pinned version.`)) return;
    await api(`/api/calliope/styles/${encodeURIComponent(selected.profile.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ archived: true }),
    });
    const ids = new Set(designVersions(selected.profile).map((item) => item.id));
    if (ids.has(state.current?.design_profile_version_id)) {
      await applyDesignProfileToSession(null);
    }
    if (ids.has(state.nextTurnDesignProfileVersionId)) {
      state.nextTurnDesignProfileVersionId = null;
    }
    state.designProfileId = null;
    state.designProfileVersionId = null;
    await loadDesignProfiles();
    if (state.designProfiles[0]) await selectDesignProfile(state.designProfiles[0].id);
    else showNewDesignProfile();
    toast("Design Profile archived");
  }

  async function forkDesignProfile() {
    const selected = selectedDesignVersion();
    if (!selected) return;
    const name = window.prompt("Name the duplicated Design Profile", `${selected.profile.name} copy`);
    if (!name?.trim()) return;
    const data = await api(
      `/api/calliope/styles/${encodeURIComponent(selected.profile.id)}/fork`,
      {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          version_id: selected.version.id,
        }),
      },
    );
    mergeDesignProfile(data.profile);
    state.designProfileId = data.profile.id;
    state.designProfileVersionId = data.profile.version.id;
    renderDesignEditor();
    toast(`Duplicated as ${data.profile.name}`);
  }

  async function loadSessions(selectId = null) {
    const data = await api("/api/calliope/sessions");
    state.sessions = data.sessions || [];
    renderSessions();
    const target = selectId || state.current?.id || state.sessions[0]?.id;
    if (target && (!state.current || state.current.id !== target)) {
      await selectSession(target);
    } else if (!target) {
      clearSession();
    }
  }

  function renderSessions() {
    const query = els.sessionSearch.value.trim().toLowerCase();
    const sessions = state.sessions.filter((session) =>
      !query || session.title.toLowerCase().includes(query)
    );
    if (!sessions.length) {
      els.sessionList.innerHTML = `<div class="session-list-empty">${
        query ? "No sessions match." : "No notebooks yet.<br>Start one and ask Calliope to make the first surface."
      }</div>`;
      return;
    }
    els.sessionList.innerHTML = sessions.map((session) => {
      const count = Number(session.surface_count || 0);
      const dots = Array.from({ length: Math.min(4, count) }, () => "<i></i>").join("");
      return `
        <button class="session-card ${state.current?.id === session.id ? "active" : ""}"
                type="button" role="listitem" data-session-id="${escapeHtml(session.id)}">
          <h3>${escapeHtml(session.title)}</h3>
          <p><span>${relativeTime(session.updated_at)}</span><span>${count} surface${count === 1 ? "" : "s"}</span></p>
          <span class="session-glyphs" aria-hidden="true">${dots}</span>
        </button>`;
    }).join("");
  }

  function clearSession() {
    clearSpatialSelections();
    state.current = null;
    state.turns = [];
    state.surfaces = [];
    state.selectedSurfaceId = null;
    state.nextTurnDesignProfileVersionId = null;
    state.cubeBuilders.clear();
    els.sessionTitle.textContent = "Choose or start a session";
    els.archiveSession.disabled = true;
    els.input.disabled = true;
    els.send.disabled = true;
    renderSelected();
    renderDesignProfileChip();
    renderSpatialSelectionTray();
    renderChat();
    renderStage();
  }

  async function selectSession(id) {
    if (state.busy) return;
    clearSpatialSelections();
    const data = await api(`/api/calliope/sessions/${encodeURIComponent(id)}`);
    state.current = data.session;
    state.turns = data.turns || [];
    state.surfaces = data.surfaces || [];
    state.selectedSurfaceId = null;
    state.nextTurnDesignProfileVersionId = null;
    state.cubeBuilders.clear();
    state.newSurfaceCount = 0;
    els.sessionTitle.textContent = state.current.title;
    els.archiveSession.disabled = false;
    els.input.disabled = false;
    els.send.disabled = false;
    renderSessions();
    renderSelected();
    renderDesignProfileChip();
    renderSpatialSelectionTray();
    renderChat(true);
    renderStage(true);
    setMobilePanel();
    requestAnimationFrame(() => els.input.focus());
  }

  async function createSession(title) {
    els.createSession.disabled = true;
    try {
      const data = await api("/api/calliope/sessions", {
        method: "POST",
        body: JSON.stringify({ title }),
      });
      els.dialog.close();
      els.newSessionTitle.value = "";
      await loadSessions(data.session.id);
    } finally {
      els.createSession.disabled = false;
    }
  }

  async function renameSession() {
    if (!state.current || state.busy) return;
    const next = window.prompt("Rename this notebook", state.current.title);
    if (!next || next.trim() === state.current.title) return;
    const data = await api(`/api/calliope/sessions/${state.current.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title: next.trim() }),
    });
    state.current = data.session;
    els.sessionTitle.textContent = state.current.title;
    await loadSessions(state.current.id);
  }

  async function archiveSession() {
    if (!state.current || state.busy) return;
    if (!window.confirm(`Archive “${state.current.title}”? Published artifacts remain shared.`)) return;
    await api(`/api/calliope/sessions/${state.current.id}`, {
      method: "PATCH",
      body: JSON.stringify({ archived: true }),
    });
    state.current = null;
    await loadSessions();
    toast("Notebook archived");
  }

  function surfacesForTurn(turnId) {
    return state.surfaces
      .filter((surface) => surface.turn_id === turnId)
      .sort((left, right) => {
        const ordinal = Number(right.ordinal || 0) - Number(left.ordinal || 0);
        if (ordinal) return ordinal;
        const created = new Date(right.created_at || 0).getTime()
          - new Date(left.created_at || 0).getTime();
        return created || String(right.id || "").localeCompare(String(left.id || ""));
      });
  }

  function thinkingState(turn) {
    if (!turn.thinking_state) {
      turn.thinking_state = THINKING_STATES[Math.floor(Math.random() * THINKING_STATES.length)];
    }
    return turn.thinking_state;
  }

  function assistantBody(turn, failed) {
    if (turn.status === "running" && !turn.assistant_message) {
      return `<div class="thinking-indicator">
        <canvas data-thinking-orb="${thinkingState(turn)}"></canvas>
      </div>`;
    }
    return safeMarkdown(
      failed ? turn.error || "That turn did not complete." : turn.assistant_message || "",
    );
  }

  function renderChat(initial = false) {
    els.chatEmpty.hidden = Boolean(state.turns.length);
    els.messages.innerHTML = state.turns.map((turn) => {
      const attachments = (turn.attachments || []).map((attachment) =>
        `<a href="${escapeHtml(attachment.url)}" target="_blank" rel="noopener">
          <img src="${escapeHtml(attachment.url)}" alt="${escapeHtml(attachment.name || "Attached image")}">
        </a>`
      ).join("");
      const surfaces = surfacesForTurn(turn.id);
      const links = surfaces.map((surface) =>
        `<button type="button" class="surface-link" data-focus-surface="${escapeHtml(surface.id)}">
          ${surfaceGlyph(surface.kind)} ${escapeHtml(surface.title)}
        </button>`
      ).join("");
      const failed = turn.status === "failed";
      return `
        <article class="message user" data-turn-id="${escapeHtml(turn.id)}">
          <div class="message-label"><span>You · ${escapeHtml(relativeTime(turn.created_at))}</span></div>
          <div class="message-body">${safeMarkdown(turn.user_message)}</div>
          ${attachments ? `<div class="message-attachments">${attachments}</div>` : ""}
        </article>
        <article class="message assistant ${turn.status === "running" ? "streaming" : ""} ${failed ? "error" : ""}"
                 data-assistant-turn-id="${escapeHtml(turn.id)}">
          <div class="message-label"><span>Calliope</span></div>
          <div class="message-body">${assistantBody(turn, failed)}</div>
          ${links ? `<div class="surface-links">${links}</div>` : ""}
        </article>`;
    }).join("");
    window.CalliopeThinkingOrbs?.mountAll(els.messages);
    if (initial) requestAnimationFrame(() => { els.messages.scrollTop = els.messages.scrollHeight; });
  }

  function surfaceGlyph(kind) {
    return ({ query: "▤", metric: "◆", cube: "▦", artifact: "▦", image: "▧", document: "▱", selection: "⌖" })[kind] || "◇";
  }

  function formatValue(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function queryColumns(surface) {
    return (surface.payload?.columns || []).map((column) =>
      typeof column === "string" ? column : column?.name
    ).filter(Boolean);
  }

  function queryRows(surface) {
    return Array.isArray(surface.payload?.rows) ? surface.payload.rows : [];
  }

  function rowValue(row, column, index) {
    if (Array.isArray(row)) return row[index];
    return row?.[column];
  }

  function isMetadataQuery(surface) {
    if (surface.payload?.metadata_query === true) return true;
    const sql = String(surface.source?.sql || "");
    return (
      /\b"?information_schema"?\s*\./i.test(sql)
      || /\b"?pg_catalog"?\s*\./i.test(sql)
      || /\b(?:from|join)\s+(?:(?:"?pg_catalog"?)\s*\.\s*)?"?pg_(?:attribute|class|constraint|database|description|extension|index(?:es)?|matviews|namespace|proc|roles|settings|stat\w*|tables|type|views)\b/i.test(sql)
      || /\bto_reg(?:class|namespace|operator|proc|procedure|type)\s*\(/i.test(sql)
    );
  }

  function usableChartNumber(value) {
    if (typeof value === "number") return Number.isFinite(value);
    return typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value));
  }

  function classifyChart(surface) {
    const columns = queryColumns(surface);
    const rows = queryRows(surface);
    if (columns.length < 2 || !rows.length) return null;
    const sample = rows.slice(0, 30);
    const numeric = columns.findIndex((column, index) => {
      const values = sample
        .map((row) => rowValue(row, column, index))
        .filter((value) => value !== null && value !== undefined && value !== "");
      return values.length > 0 && values.every(usableChartNumber);
    });
    if (numeric < 0) return null;
    const temporal = columns.findIndex((column, index) => {
      if (index === numeric) return false;
      const values = sample
        .map((row) => rowValue(row, column, index))
        .filter((value) => value !== null && value !== undefined && value !== "");
      return values.length > 0 && values.every((value) => {
        return typeof value === "string" && /[-/:T]/.test(value) && Number.isFinite(Date.parse(value));
      });
    });
    const category = temporal >= 0
      ? temporal
      : columns.findIndex((column, index) =>
        index !== numeric && sample.some((row) => {
          const value = rowValue(row, column, index);
          return value !== null && value !== undefined && value !== "" && typeof value !== "object";
        })
      );
    if (category < 0) return null;
    const points = rows.slice(0, temporal >= 0 ? 80 : 30).map((row) => ({
      label: formatValue(rowValue(row, columns[category], category)),
      value: Number(rowValue(row, columns[numeric], numeric)),
      x: temporal >= 0 ? Date.parse(rowValue(row, columns[category], category)) : null,
    })).filter((point) => Number.isFinite(point.value) && (temporal < 0 || Number.isFinite(point.x)));
    if (!points.length) return null;
    return {
      type: temporal >= 0 ? "line" : "bar",
      points,
      xLabel: columns[category],
      yLabel: columns[numeric],
    };
  }

  function renderChart(surface, chart = classifyChart(surface)) {
    if (!chart || !chart.points.length) {
      return `<div class="chart-wrap"><div class="chart-empty">No useful numeric relationship was found.<br>Use the table or ask Calliope for a chart-ready query.</div></div>`;
    }
    const W = 640, H = 230, left = 50, right = 14, top = 15, bottom = 35;
    const values = chart.points.map((point) => point.value);
    const max = Math.max(...values, 0);
    const min = Math.min(...values, 0);
    const range = max - min || 1;
    const y = (value) => top + (max - value) / range * (H - top - bottom);
    const grid = [0, .25, .5, .75, 1].map((fraction) => {
      const value = max - range * fraction;
      const yy = y(value);
      return `<line class="chart-grid" x1="${left}" y1="${yy}" x2="${W - right}" y2="${yy}"/>
        <text class="chart-label" x="${left - 7}" y="${yy + 3}" text-anchor="end">${escapeHtml(compactNumber(value))}</text>`;
    }).join("");
    let marks = "";
    if (chart.type === "bar") {
      const width = (W - left - right) / chart.points.length;
      marks = chart.points.map((point, index) => {
        const yy = y(Math.max(point.value, 0));
        const zero = y(0);
        const height = Math.max(1, Math.abs(zero - y(point.value)));
        const label = point.label.length > 14 ? `${point.label.slice(0, 13)}…` : point.label;
        return `<rect class="chart-bar" x="${left + index * width + 2}" y="${Math.min(yy, zero)}"
                  width="${Math.max(2, width - 4)}" height="${height}">
                  <title>${escapeHtml(point.label)} · ${escapeHtml(formatValue(point.value))}</title>
                </rect>
                ${chart.points.length <= 12 ? `<text class="chart-label" x="${left + index * width + width / 2}" y="${H - 13}" text-anchor="middle">${escapeHtml(label)}</text>` : ""}`;
      }).join("");
    } else {
      const sorted = [...chart.points].sort((a, b) => a.x - b.x);
      const minX = Math.min(...sorted.map((point) => point.x));
      const maxX = Math.max(...sorted.map((point) => point.x));
      const xRange = maxX - minX || 1;
      const x = (value) => left + (value - minX) / xRange * (W - left - right);
      const path = sorted.map((point, index) => `${index ? "L" : "M"} ${x(point.x)} ${y(point.value)}`).join(" ");
      const area = `${path} L ${x(sorted.at(-1).x)} ${y(0)} L ${x(sorted[0].x)} ${y(0)} Z`;
      marks = `<path class="chart-area" d="${area}"/><path class="chart-line" d="${path}"/>` +
        sorted.map((point) => `<circle class="chart-point" cx="${x(point.x)}" cy="${y(point.value)}" r="3">
          <title>${escapeHtml(point.label)} · ${escapeHtml(formatValue(point.value))}</title></circle>`).join("");
    }
    return `<div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(chart.yLabel)} by ${escapeHtml(chart.xLabel)}">
      ${grid}<line class="chart-axis" x1="${left}" y1="${H - bottom}" x2="${W - right}" y2="${H - bottom}"/>
      ${marks}</svg></div>`;
  }

  function compactNumber(value) {
    const abs = Math.abs(value);
    if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}b`;
    if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}m`;
    if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
    return Number(value.toFixed(2)).toLocaleString();
  }

  function metricDatum(value, name = "") {
    if (usableChartNumber(value)) {
      return { number: Number(value), raw: value, key: name };
    }
    if (Array.isArray(value)) {
      return value.length === 1 ? metricDatum(value[0], name) : { number: null, raw: value, key: name };
    }
    if (!value || typeof value !== "object") {
      return { number: null, raw: value, key: name };
    }
    const entries = Object.entries(value);
    const priorities = ["value", "result", "total", "count", "amount", "revenue", "rate", "percentage", "percent", "score"];
    for (const key of priorities) {
      const match = entries.find(([candidate]) => candidate.toLowerCase() === key);
      if (match && usableChartNumber(match[1])) {
        return { number: Number(match[1]), raw: match[1], key: match[0] };
      }
    }
    const match = entries.find(([key, candidate]) =>
      !/(?:^|_)(?:id|version|year|month|day)(?:_|$)/i.test(key) && usableChartNumber(candidate)
    );
    return match
      ? { number: Number(match[1]), raw: match[1], key: match[0] }
      : { number: null, raw: value, key: name };
  }

  function formatMetricDatum(datum, title) {
    if (!Number.isFinite(datum.number)) {
      return Array.isArray(datum.raw) || (datum.raw && typeof datum.raw === "object")
        ? JSON.stringify(datum.raw)
        : formatValue(datum.raw);
    }
    const hint = `${datum.key || ""} ${title || ""}`.toLowerCase();
    if (/(?:percent|percentage|pct|rate)/.test(hint) && Math.abs(datum.number) <= 1.25) {
      return new Intl.NumberFormat(undefined, {
        style: "percent",
        maximumFractionDigits: 1,
      }).format(datum.number);
    }
    if (/(?:revenue|sales|amount|arr|mrr|currency|dollar)/.test(hint)) {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: "USD",
        notation: Math.abs(datum.number) >= 10_000 ? "compact" : "standard",
        maximumFractionDigits: 1,
      }).format(datum.number);
    }
    return new Intl.NumberFormat(undefined, {
      notation: Math.abs(datum.number) >= 100_000 ? "compact" : "standard",
      maximumFractionDigits: 2,
    }).format(datum.number);
  }

  function metricTimeline(payload) {
    const source = payload?.observations || payload?.history || payload?.timeline || [];
    if (!Array.isArray(source)) return [];
    const points = source.map((item, index) => {
      const record = item && typeof item === "object" ? item : { value: item };
      const datum = metricDatum(record.value ?? record.result ?? record);
      const label = record.data_as_of || record.observed_at || record.created_at || "";
      return {
        value: datum.number,
        label: String(label || `Observation ${index + 1}`),
        time: Number.isFinite(Date.parse(label)) ? Date.parse(label) : null,
      };
    }).filter((point) => Number.isFinite(point.value));
    if (points.length > 1 && points.every((point) => Number.isFinite(point.time))) {
      points.sort((left, right) => left.time - right.time);
    } else {
      points.reverse();
    }
    return points.slice(-40);
  }

  function renderMetricTrend(points) {
    if (points.length < 2) return "";
    const W = 520, H = 170, pad = 8;
    const values = points.map((point) => point.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || Math.abs(max) || 1;
    const x = (index) => pad + index / Math.max(1, points.length - 1) * (W - pad * 2);
    const y = (value) => pad + (max - value) / range * (H - pad * 2);
    const path = points.map((point, index) =>
      `${index ? "L" : "M"} ${x(index).toFixed(2)} ${y(point.value).toFixed(2)}`
    ).join(" ");
    const area = `${path} L ${x(points.length - 1).toFixed(2)} ${H} L ${x(0).toFixed(2)} ${H} Z`;
    const last = points.at(-1);
    return `<div class="metric-trend" aria-hidden="true"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <path class="metric-area" d="${area}"></path>
      <path class="metric-line" d="${path}"></path>
      <circle class="metric-point" cx="${x(points.length - 1)}" cy="${y(last.value)}" r="4">
        <title>${escapeHtml(last.label)} · ${escapeHtml(formatValue(last.value))}</title>
      </circle>
    </svg></div>`;
  }

  function renderQuery(surface) {
    const columns = queryColumns(surface);
    const rows = queryRows(surface);
    const chart = classifyChart(surface);
    const defaultView = chart && !isMetadataQuery(surface) ? "chart" : "table";
    const table = `<div class="table-wrap"><table class="data-table"><thead><tr>${
      columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")
    }</tr></thead><tbody>${
      rows.map((row) => `<tr>${columns.map((column, index) => {
        const value = formatValue(rowValue(row, column, index));
        return `<td title="${escapeHtml(value)}">${escapeHtml(value)}</td>`;
      }).join("")}</tr>`).join("")
    }</tbody></table></div>`;
    return `
      <div class="surface-tabs">
        ${chart ? `<button type="button" class="${defaultView === "chart" ? "active" : ""}" data-view="chart">Chart</button>` : ""}
        <button type="button" class="${defaultView === "table" ? "active" : ""}" data-view="table">Table</button>
        <button type="button" data-view="sql">SQL</button>
      </div>
      <div class="query-meta">
        <span><b>${escapeHtml(surface.payload?.row_count ?? rows.length)}</b> rows</span>
        ${surface.payload?.engine ? `<span><b>${escapeHtml(surface.payload.engine)}</b> engine</span>` : ""}
        ${surface.payload?.elapsed_ms != null ? `<span><b>${escapeHtml(surface.payload.elapsed_ms)}ms</b></span>` : ""}
        ${surface.payload?.truncated ? "<span>preview truncated</span>" : ""}
      </div>
      ${chart ? `<div class="query-view" data-query-view="chart" ${defaultView === "chart" ? "" : "hidden"}>${renderChart(surface, chart)}</div>` : ""}
      <div class="query-view" data-query-view="table" ${defaultView === "table" ? "" : "hidden"}>${table}</div>
      <div class="query-view" data-query-view="sql" hidden><pre class="sql-view">${escapeHtml(surface.source?.sql || "SQL unavailable")}</pre></div>`;
  }

  function renderMetric(surface) {
    const payload = surface.payload || {};
    const datum = metricDatum(payload.result, payload.name || surface.title);
    const display = formatMetricDatum(datum, surface.title);
    const points = metricTimeline(payload);
    const first = points[0]?.value;
    const last = points.at(-1)?.value;
    const change = Number.isFinite(first) && Number.isFinite(last) && first !== 0
      ? (last - first) / Math.abs(first)
      : null;
    const delta = change == null
      ? ""
      : `<span class="metric-delta ${change < 0 ? "down" : ""}">${
          change > 0 ? "▲" : change < 0 ? "▼" : "–"
        } ${escapeHtml(Math.abs(change * 100).toFixed(1))}%</span>`;
    const latest = Array.isArray(payload.observations) ? payload.observations[0] : null;
    const status = latest?.status || (latest?.verdict === true ? "passing" : latest?.verdict === false ? "breaching" : "governed metric");
    return `<div class="metric-body">
      <div class="metric-grid" aria-hidden="true"></div>
      ${renderMetricTrend(points)}
      ${points.length < 2 ? `<span class="metric-snapshot">Current snapshot</span>` : ""}
      <div class="metric-content">
        <div class="metric-kicker">${escapeHtml(status)}</div>
        <div class="metric-value">${escapeHtml(display)}${delta}</div>
        <div class="metric-caption">
          <span><b>${escapeHtml(surface.title)}</b>${
            payload.data_as_of ? ` · ${escapeHtml(payload.data_as_of)}` : ""
          }</span>
          <span>${points.length > 1 ? `${points.length} observations` : "live value"}</span>
        </div>
      </div>
    </div>`;
  }

  function normalizeCubeFields(payload) {
    const raw = payload?.columns;
    if (Array.isArray(raw)) {
      return raw.map((field) => typeof field === "string"
        ? { name: field, type: "", kind: "field", groupable: true, numeric: false }
        : {
            name: field?.column_name || field?.name || field?.column || "field",
            type: field?.data_type || field?.type || "",
            kind: field?.kind || "field",
            groupable: typeof field?.groupable === "boolean" ? field.groupable : null,
            numeric: typeof field?.numeric === "boolean" ? field.numeric : null,
            doc: field?.doc || field?.semantics || "",
          });
    }
    if (raw && typeof raw === "object") {
      return Object.entries(raw).map(([name, detail]) => ({
        name,
        type: typeof detail === "string" ? detail : detail?.type || detail?.data_type || "",
        kind: typeof detail === "object" ? detail?.kind || "field" : "field",
        groupable: typeof detail === "object" && typeof detail?.groupable === "boolean"
          ? detail.groupable
          : null,
        numeric: typeof detail === "object" && typeof detail?.numeric === "boolean"
          ? detail.numeric
          : null,
        doc: typeof detail === "object" ? detail?.doc || detail?.semantics || "" : "",
      }));
    }
    return [];
  }

  function cubeFieldIsNumeric(field) {
    if (typeof field.numeric === "boolean") return field.numeric;
    const type = String(field.type || "").toLowerCase();
    return /^(?:bigint|decimal|double precision|integer|money|numeric|real|smallint)$/.test(type)
      || /^(?:float|int|number)/.test(type);
  }

  function cubeFieldIsGroupable(field) {
    if (typeof field.groupable === "boolean") return field.groupable;
    if (["dimension", "time", "key"].includes(String(field.kind || "").toLowerCase())) return true;
    return !cubeFieldIsNumeric(field);
  }

  function cubeBuilderFor(surface, fields) {
    const dimensions = fields.filter(cubeFieldIsGroupable);
    const numericFields = fields.filter(cubeFieldIsNumeric);
    const prior = state.cubeBuilders.get(surface.id);
    const dimensionNames = new Set(dimensions.map((field) => field.name));
    const numericNames = new Set(numericFields.map((field) => field.name));
    const next = prior || {
      rows: dimensions[0] ? [dimensions[0].name] : [],
      cols: [],
      measures: numericFields[0]
        ? [{ field: numericFields[0].name, aggregate: "sum" }]
        : [{ field: null, aggregate: "count" }],
      result: null,
      error: "",
      requestId: 0,
      timer: null,
      minHeight: 340,
    };
    next.rows = [...new Set(
      (Array.isArray(next.rows) ? next.rows : [next.rows]).filter((name) => dimensionNames.has(name))
    )];
    next.cols = [...new Set(
      (Array.isArray(next.cols) ? next.cols : [next.cols]).filter(
        (name) => dimensionNames.has(name) && !next.rows.includes(name)
      )
    )];
    next.measures = (Array.isArray(next.measures) ? next.measures : [])
      .map((spec) => ({
        field: spec?.field || null,
        aggregate: spec?.field ? spec?.aggregate || "sum" : "count",
      }))
      .filter((spec, index, all) =>
        (spec.field === null || numericNames.has(spec.field))
        && all.findIndex((item) => item.field === spec.field) === index
      );
    if (!next.measures.length && !numericFields.length) {
      next.measures = [{ field: null, aggregate: "count" }];
    }
    state.cubeBuilders.set(surface.id, next);
    return { config: next, dimensions, numericFields };
  }

  function cubeFieldRole(field, config) {
    const rowIndex = config.rows.indexOf(field.name);
    const colIndex = config.cols.indexOf(field.name);
    const measure = config.measures.find((item) => item.field === field.name);
    if (rowIndex >= 0) return `Rows ${rowIndex + 1}`;
    if (colIndex >= 0) return `Columns ${colIndex + 1}`;
    if (measure) return `Σ ${measure.aggregate}`;
    return cubeFieldIsNumeric(field) ? "Number" : String(field.kind || "Dimension");
  }

  function cubeFieldRoleClass(field, config) {
    if (config.rows.includes(field.name)) return "is-rows";
    if (config.cols.includes(field.name)) return "is-cols";
    if (config.measures.some((item) => item.field === field.name)) return "is-measure";
    return "";
  }

  function cubeAggregateOptions(selected) {
    return [
      ["sum", "Sum"],
      ["avg", "Average"],
      ["min", "Minimum"],
      ["max", "Maximum"],
      ["count", "Count"],
      ["count_distinct", "Distinct"],
    ].map(([value, label]) => `<option value="${value}" ${
      selected === value ? "selected" : ""
    }>${label}</option>`).join("");
  }

  function cubeDimensionShelf(label, name, values, optional = false) {
    return `<section class="cube-shelf cube-shelf-${name}">
      <header><b>${label}</b>${optional ? "<span>Optional · creates a cross-tab</span>" : ""}</header>
      <div class="cube-shelf-items">${values.length
        ? values.map((field) => `<span class="cube-shelf-chip">
            <b>${escapeHtml(field)}</b>
            <button type="button" data-cube-remove-field="${escapeHtml(field)}"
              data-cube-remove-shelf="${name}" aria-label="Remove ${escapeHtml(field)} from ${label}">×</button>
          </span>`).join("")
        : `<span class="cube-shelf-empty">${name === "cols" ? "Grouped table" : "Overall summary"}</span>`
      }</div>
    </section>`;
  }

  function cubeValueShelf(config) {
    const values = config.measures.map((spec) => {
      const key = spec.field || "__rows__";
      return `<span class="cube-shelf-chip cube-value-chip">
        <b>${escapeHtml(spec.field || "Rows")}</b>
        ${spec.field
          ? `<select data-cube-measure-aggregate="${escapeHtml(key)}"
              aria-label="Aggregate ${escapeHtml(spec.field)}">${cubeAggregateOptions(spec.aggregate)}</select>`
          : `<i>Count</i>`
        }
        <button type="button" data-cube-remove-measure="${escapeHtml(key)}"
          aria-label="Remove ${escapeHtml(spec.field || "row count")}">×</button>
      </span>`;
    }).join("");
    const hasCount = config.measures.some((spec) => spec.field === null);
    return `<section class="cube-shelf cube-shelf-values">
      <header><b>Values</b><span>One or more aggregates</span></header>
      <div class="cube-shelf-items">${values || `<span class="cube-shelf-empty">Choose a number below</span>`}
        ${hasCount ? "" : `<button class="cube-add-count" type="button" data-cube-add-count>+ Count rows</button>`}
      </div>
    </section>`;
  }

  function renderCubeConfiguration(fields, config) {
    return `<div class="cube-shelves">
      ${cubeDimensionShelf("Rows", "rows", config.rows)}
      ${cubeDimensionShelf("Columns", "cols", config.cols, true)}
      ${cubeValueShelf(config)}
    </div>
    <div class="cube-palette-note">Dimensions cycle through Rows → Columns → Off. Numbers toggle in Values.</div>
    <div class="cube-schema">${fields.map((field) => `<button class="cube-field ${
      cubeFieldRoleClass(field, config)
    }" type="button" data-cube-field="${escapeHtml(field.name)}" data-cube-numeric="${
      cubeFieldIsNumeric(field)
    }" data-cube-groupable="${cubeFieldIsGroupable(field)}" aria-pressed="${
      Boolean(cubeFieldRoleClass(field, config))
    }" title="${escapeHtml(field.doc || field.name)}">
      <b title="${escapeHtml(field.name)}">${escapeHtml(field.name)}</b>
      <span data-cube-field-role>${escapeHtml(cubeFieldRole(field, config))}${
        field.type ? ` · ${escapeHtml(field.type)}` : ""
      }</span>
    </button>`).join("")}</div>`;
  }

  function formatCubeValue(value) {
    if (!usableChartNumber(value)) return formatValue(value);
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function cubeHeatCell(value, maximum, title, extraClass = "") {
    const numeric = usableChartNumber(value) ? Number(value) : null;
    const heat = numeric == null ? 0 : 9 + Math.abs(numeric) / Math.max(1, maximum) * 43;
    return `<td class="cube-cell ${numeric != null && numeric < 0 ? "negative" : ""} ${extraClass}"
      data-cube-value="${numeric ?? ""}" style="--heat:${heat.toFixed(1)}%"
      title="${escapeHtml(title)}">${escapeHtml(formatCubeValue(value))}</td>`;
  }

  function cubeResultToolbar(summary, sortOptions) {
    return `<div class="cube-toolbar">
      <span class="cube-axis">${summary}</span>
      <div class="cube-tools">
        <input class="cube-search" type="search" data-cube-search placeholder="Filter rows…" aria-label="Filter cube rows">
        <select class="cube-sort" data-cube-sort aria-label="Sort cube rows">
          <option value="label">Label ↑</option>
          ${sortOptions}
        </select>
        <button class="cube-toggle" type="button" data-cube-heat aria-pressed="true">Heat</button>
      </div>
    </div>`;
  }

  function renderCubeTable(payload) {
    const columns = Array.isArray(payload.table_columns) ? payload.table_columns : [];
    const rows = Array.isArray(payload.table_rows) ? payload.table_rows : [];
    const dimensions = Array.isArray(payload.row_dimensions) ? payload.row_dimensions : [];
    const measures = Array.isArray(payload.measures) ? payload.measures : [];
    if (!rows.length || !columns.length) {
      return `<div class="cube-empty">This grouped table returned no rows.</div>`;
    }
    const maxima = Object.fromEntries(measures.map((measure) => [
      measure.key,
      Math.max(1, ...rows
        .map((row) => row?.values?.[measure.key])
        .filter(usableChartNumber)
        .map((value) => Math.abs(Number(value)))),
    ]));
    const renderedRows = rows.map((row) => {
      const label = dimensions.map((name) => row?.dimensions?.[name]).join(" · ") || "Overall";
      return `<tr data-cube-row data-cube-label="${escapeHtml(label)}">
        ${dimensions.map((name) => `<td title="${escapeHtml(row?.dimensions?.[name] ?? "")}">${
          escapeHtml(row?.dimensions?.[name] ?? "—")
        }</td>`).join("")}
        ${measures.map((measure) => cubeHeatCell(
          row?.values?.[measure.key],
          maxima[measure.key],
          `${measure.label} · ${formatValue(row?.values?.[measure.key])}`,
        )).join("")}
      </tr>`;
    }).join("");
    const sortOptions = measures.map((measure, index) => {
      const cellIndex = dimensions.length + index;
      return `<option value="cell:${cellIndex}">${escapeHtml(measure.label)} ↓</option>`;
    }).join("");
    const summary = `<b>${escapeHtml(dimensions.join(" › ") || "Overall")}</b><i>·</i>${
      escapeHtml(measures.map((measure) => measure.label).join(" · "))
    }`;
    return `<div class="cube-shell heat-on" data-cube data-cube-mode="table">
      ${cubeResultToolbar(summary, sortOptions)}
      <div class="cube-table-wrap"><table class="cube-table">
        <thead><tr>
          ${dimensions.map((name) => `<th>${escapeHtml(name)}</th>`).join("")}
          ${measures.map((measure, index) => `<th data-cube-column="${
            dimensions.length + index
          }" title="Sort by ${escapeHtml(measure.label)}">${escapeHtml(measure.label)}</th>`).join("")}
        </tr></thead>
        <tbody>${renderedRows}</tbody>
        <tfoot><tr>
          ${dimensions.map((_, index) => `<td>${index === 0 ? "Overall" : ""}</td>`).join("")}
          ${measures.map((measure) => `<td>${escapeHtml(formatCubeValue(
            payload.grand_totals?.[measure.key]
          ))}</td>`).join("")}
        </tr></tfoot>
      </table></div>
    </div>`;
  }

  function legacyCubeCrosstab(payload) {
    const columns = Array.isArray(payload.columns) ? payload.columns.map(String) : [];
    const measureKey = `${payload.aggregate || "value"}:${payload.measure || payload.value_label || "value"}`;
    return {
      ...payload,
      display_mode: "crosstab",
      row_dimensions: [payload.rows_dim || "Rows"],
      measures: [{
        key: measureKey,
        field: payload.measure || null,
        aggregate: payload.aggregate || "",
        label: [payload.aggregate, payload.measure || payload.value_label || "Value"].filter(Boolean).join(" "),
      }],
      value_columns: columns.map((column) => ({
        key: column,
        label: column,
        measure_key: measureKey,
      })),
      matrix: (payload.matrix || []).map((row) => ({
        ...row,
        dimensions: { [payload.rows_dim || "Rows"]: row.row },
        totals: { [measureKey]: row.total },
      })),
      grand_totals: { [measureKey]: payload.grand_total },
    };
  }

  function renderCubeCrosstab(rawPayload) {
    const payload = rawPayload.value_columns ? rawPayload : legacyCubeCrosstab(rawPayload);
    const columns = Array.isArray(payload.value_columns) ? payload.value_columns : [];
    const matrix = Array.isArray(payload.matrix) ? payload.matrix : [];
    const dimensions = Array.isArray(payload.row_dimensions) ? payload.row_dimensions : [];
    const measures = Array.isArray(payload.measures) ? payload.measures : [];
    if (!matrix.length || !columns.length) {
      return `<div class="cube-empty">This cross-tab returned no cells.</div>`;
    }
    const maxima = Object.fromEntries(measures.map((measure) => [
      measure.key,
      Math.max(1, ...matrix.flatMap((row) =>
        columns
          .filter((column) => column.measure_key === measure.key)
          .map((column) => row?.cells?.[column.key])
          .filter(usableChartNumber)
          .map((value) => Math.abs(Number(value)))
      )),
    ]));
    const rows = matrix.map((row) => {
      const label = dimensions.map((name) => row?.dimensions?.[name]).join(" · ")
        || String(row?.row ?? "Overall");
      return `<tr data-cube-row data-cube-label="${escapeHtml(label)}">
        ${dimensions.map((name) => `<td title="${escapeHtml(row?.dimensions?.[name] ?? "")}">${
          escapeHtml(row?.dimensions?.[name] ?? "—")
        }</td>`).join("")}
        ${columns.map((column) => cubeHeatCell(
          row?.cells?.[column.key],
          maxima[column.measure_key] || 1,
          `${column.label} · ${formatValue(row?.cells?.[column.key])}`,
        )).join("")}
        ${measures.map((measure) => cubeHeatCell(
          row?.totals?.[measure.key] ?? row?.total,
          Math.max(1, ...matrix
            .map((item) => item?.totals?.[measure.key] ?? item?.total)
            .filter(usableChartNumber)
            .map((value) => Math.abs(Number(value)))),
          `Overall · ${measure.label}`,
          "cube-total",
        )).join("")}
      </tr>`;
    }).join("");
    const sortOptions = [
      ...columns.map((column, index) => `<option value="cell:${
        dimensions.length + index
      }">${escapeHtml(column.label)} ↓</option>`),
      ...measures.map((measure, index) => `<option value="cell:${
        dimensions.length + columns.length + index
      }">Overall · ${escapeHtml(measure.label)} ↓</option>`),
    ].join("");
    const summary = `<b>${escapeHtml(dimensions.join(" › ") || "Overall")}</b><i>×</i><b>${
      escapeHtml((payload.column_dimensions || [payload.cols_dim]).filter(Boolean).join(" › ") || "Columns")
    }</b><i>·</i>${escapeHtml(measures.map((measure) => measure.label).join(" · "))}`;
    return `<div class="cube-shell heat-on" data-cube data-cube-mode="crosstab">
      ${cubeResultToolbar(summary, sortOptions)}
      <div class="cube-table-wrap"><table class="cube-table">
        <thead><tr>
          ${dimensions.map((name) => `<th>${escapeHtml(name)}</th>`).join("")}
          ${columns.map((column, index) => `<th data-cube-column="${
            dimensions.length + index
          }" title="Sort by ${escapeHtml(column.label)}">${escapeHtml(column.label)}</th>`).join("")}
          ${measures.map((measure, index) => `<th data-cube-column="${
            dimensions.length + columns.length + index
          }">Overall · ${escapeHtml(measure.label)}</th>`).join("")}
        </tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr>
          ${dimensions.map((_, index) => `<td>${index === 0 ? "Overall" : ""}</td>`).join("")}
          ${columns.map((column) => `<td>${escapeHtml(formatCubeValue(
            payload.col_totals?.[column.key]
          ))}</td>`).join("")}
          ${measures.map((measure) => `<td>${escapeHtml(formatCubeValue(
            payload.grand_totals?.[measure.key] ?? payload.grand_total
          ))}</td>`).join("")}
        </tr></tfoot>
      </table></div>
    </div>`;
  }

  function renderCubeResult(payload) {
    return payload.display_mode === "table"
      ? renderCubeTable(payload)
      : renderCubeCrosstab(payload);
  }

  function renderCubePivot(payload) {
    return renderCubeResult(payload);
  }

  function renderCubeSchema(surface) {
    const payload = surface.payload || {};
    const fields = normalizeCubeFields(payload);
    if (!fields.length) {
      return `<div class="cube-empty">Cube metadata is available, but no fields were returned.</div>`;
    }
    const cube = payload.name || payload.cube || "";
    const { config } = cubeBuilderFor(surface, fields);
    return `<div class="cube-shell cube-builder ${config.result ? "has-result" : ""}"
      data-cube-builder="${escapeHtml(surface.id)}" data-cube-name="${escapeHtml(cube)}">
      <div class="cube-toolbar">
        <span class="cube-axis">Grain <b>${escapeHtml(payload.grain || "not declared")}</b></span>
        <span class="cube-axis"><i>◆</i> ${escapeHtml(fields.length)} fields</span>
        <span class="cube-auto-label">Auto-updates</span>
      </div>
      <div data-cube-config>${renderCubeConfiguration(fields, config)}</div>
      <div class="cube-refresh-status ${config.error ? "error" : ""}" data-cube-status>${
        escapeHtml(config.error || "")
      }</div>
      <div class="cube-result" data-cube-result style="min-height:${Math.max(
        340,
        Number(config.minHeight || 0),
      )}px">${
        config.result
          ? renderCubeResult(config.result)
          : config.error
            ? `<div class="cube-empty cube-error">${escapeHtml(config.error)}</div>`
            : `<div class="cube-empty">Preparing the grouped table…</div>`
      }</div>
    </div>`;
  }

  function renderCube(surface) {
    const payload = surface.payload || {};
    if (payload.mode === "schema") {
      return renderCubeSchema(surface);
    }
    return renderCubeResult(payload);
  }

  function applyCubeView(shell) {
    if (!shell) return;
    const query = $("[data-cube-search]", shell)?.value.trim().toLowerCase() || "";
    const sort = $("[data-cube-sort]", shell)?.value || "label";
    const body = $(".cube-table tbody", shell);
    if (!body) return;
    const rows = $$("[data-cube-row]", body);
    rows.forEach((row) => {
      row.hidden = Boolean(query) && !String(row.dataset.cubeLabel || "").toLowerCase().includes(query);
    });
    rows.sort((left, right) => {
      if (sort === "label") {
        return String(left.dataset.cubeLabel || "").localeCompare(String(right.dataset.cubeLabel || ""));
      }
      const cellIndex = sort.startsWith("cell:") ? Number(sort.split(":")[1]) : null;
      const leftValue = Number(left.children[cellIndex]?.dataset.cubeValue);
      const rightValue = Number(right.children[cellIndex]?.dataset.cubeValue);
      if (!Number.isFinite(leftValue)) return 1;
      if (!Number.isFinite(rightValue)) return -1;
      return rightValue - leftValue;
    });
    rows.forEach((row) => body.append(row));
  }

  function cubeBuilderContext(builder) {
    const config = state.cubeBuilders.get(builder?.dataset.cubeBuilder);
    const surface = state.surfaces.find((item) => item.id === builder?.dataset.cubeBuilder);
    return { config, surface, fields: normalizeCubeFields(surface?.payload || {}) };
  }

  function refreshCubeConfiguration(builder) {
    const { config, fields } = cubeBuilderContext(builder);
    if (!builder || !config) return;
    const target = $("[data-cube-config]", builder);
    if (target) target.innerHTML = renderCubeConfiguration(fields, config);
  }

  function cubeBuilderValid(builder, config) {
    return Boolean(builder?.dataset.cubeName && config?.measures?.length);
  }

  function scheduleCubeBuilder(builder, delay = 140) {
    const { config } = cubeBuilderContext(builder);
    if (!builder || !config) return;
    clearTimeout(config.timer);
    config.requestId = Number(config.requestId || 0) + 1;
    const requestId = config.requestId;
    const status = $("[data-cube-status]", builder);
    if (!cubeBuilderValid(builder, config)) {
      config.timer = null;
      builder.classList.add("cube-invalid");
      builder.classList.remove("is-loading");
      builder.removeAttribute("aria-busy");
      if (status) status.textContent = "Add at least one value to calculate.";
      return;
    }
    builder.classList.remove("cube-invalid");
    if (status) {
      status.classList.remove("error");
      status.textContent = config.result ? "Updating…" : "Calculating…";
    }
    config.timer = setTimeout(() => runCubeBuilder(builder, requestId), delay);
  }

  function initializeCubeBuilders() {
    $$("[data-cube-builder]", els.stage).forEach((builder) => {
      const { config } = cubeBuilderContext(builder);
      if (config && !config.result && !config.timer) scheduleCubeBuilder(builder, 0);
    });
  }

  function selectCubeField(button) {
    const builder = button.closest("[data-cube-builder]");
    const { config } = cubeBuilderContext(builder);
    if (!builder || !config) return;
    const name = button.dataset.cubeField;
    const numeric = button.dataset.cubeNumeric === "true";
    const groupable = button.dataset.cubeGroupable === "true";
    if (numeric) {
      const existing = config.measures.findIndex((item) => item.field === name);
      if (existing >= 0) config.measures.splice(existing, 1);
      else config.measures.push({ field: name, aggregate: "sum" });
    } else if (groupable) {
      const rowIndex = config.rows.indexOf(name);
      const colIndex = config.cols.indexOf(name);
      if (rowIndex >= 0) {
        config.rows.splice(rowIndex, 1);
        config.cols.push(name);
      } else if (colIndex >= 0) {
        config.cols.splice(colIndex, 1);
      } else {
        config.rows.push(name);
      }
    }
    config.error = "";
    refreshCubeConfiguration(builder);
    scheduleCubeBuilder(builder);
  }

  function removeCubeField(button) {
    const builder = button.closest("[data-cube-builder]");
    const { config } = cubeBuilderContext(builder);
    if (!builder || !config) return;
    const shelf = button.dataset.cubeRemoveShelf;
    config[shelf] = (config[shelf] || []).filter(
      (name) => name !== button.dataset.cubeRemoveField
    );
    refreshCubeConfiguration(builder);
    scheduleCubeBuilder(builder);
  }

  function removeCubeMeasure(button) {
    const builder = button.closest("[data-cube-builder]");
    const { config } = cubeBuilderContext(builder);
    if (!builder || !config) return;
    const field = button.dataset.cubeRemoveMeasure === "__rows__"
      ? null
      : button.dataset.cubeRemoveMeasure;
    config.measures = config.measures.filter((item) => item.field !== field);
    refreshCubeConfiguration(builder);
    scheduleCubeBuilder(builder);
  }

  function addCubeRowCount(button) {
    const builder = button.closest("[data-cube-builder]");
    const { config } = cubeBuilderContext(builder);
    if (!builder || !config || config.measures.some((item) => item.field === null)) return;
    config.measures.push({ field: null, aggregate: "count" });
    refreshCubeConfiguration(builder);
    scheduleCubeBuilder(builder);
  }

  async function runCubeBuilder(builder, requestId) {
    const id = builder?.dataset.cubeBuilder;
    const config = state.cubeBuilders.get(id);
    const surface = state.surfaces.find((item) => item.id === id);
    if (!config || !surface || config.requestId !== requestId) return;
    config.timer = null;
    const result = $("[data-cube-result]", builder);
    if (result) {
      config.minHeight = Math.max(
        340,
        Math.min(560, Math.ceil(result.getBoundingClientRect().height || 0)),
      );
      result.style.minHeight = `${config.minHeight}px`;
    }
    builder.classList.add("is-loading");
    builder.setAttribute("aria-busy", "true");
    try {
      const data = await api(
        `/api/calliope/cubes/${encodeURIComponent(builder.dataset.cubeName)}/pivot`,
        {
          method: "POST",
          body: JSON.stringify({
            rows: config.rows,
            cols: config.cols,
            measures: config.measures,
          }),
        },
      );
      if (config.requestId !== requestId) return;
      config.result = data;
      config.error = "";
      const live = $(`[data-cube-builder="${CSS.escape(builder.dataset.cubeBuilder)}"]`, els.stage);
      const liveResult = $("[data-cube-result]", live);
      const priorSearch = $("[data-cube-search]", liveResult)?.value || "";
      const priorSort = $("[data-cube-sort]", liveResult)?.value || "label";
      const priorHeat = $("[data-cube-heat]", liveResult)?.getAttribute("aria-pressed") !== "false";
      if (liveResult) {
        liveResult.innerHTML = renderCubeResult(data);
        liveResult.style.minHeight = `${config.minHeight}px`;
        const search = $("[data-cube-search]", liveResult);
        const sort = $("[data-cube-sort]", liveResult);
        const heat = $("[data-cube-heat]", liveResult);
        if (search) search.value = priorSearch;
        if (sort && [...sort.options].some((option) => option.value === priorSort)) sort.value = priorSort;
        if (heat) heat.setAttribute("aria-pressed", String(priorHeat));
        $("[data-cube]", liveResult)?.classList.toggle("heat-on", priorHeat);
        applyCubeView($("[data-cube]", liveResult));
      }
      live?.classList.add("has-result");
      const status = $("[data-cube-status]", live);
      if (status) {
        status.classList.remove("error");
        status.textContent = "";
      }
    } catch (error) {
      if (config.requestId !== requestId) return;
      config.error = error.message || "Could not build this pivot";
      const live = $(`[data-cube-builder="${CSS.escape(builder.dataset.cubeBuilder)}"]`, els.stage);
      const liveResult = $("[data-cube-result]", live);
      if (liveResult && !config.result) {
        liveResult.innerHTML = `<div class="cube-empty cube-error">${escapeHtml(config.error)}</div>`;
      }
      const status = $("[data-cube-status]", live);
      if (status) {
        status.classList.add("error");
        status.textContent = config.error;
      }
      toast(config.error, true);
    } finally {
      if (config.requestId !== requestId) return;
      const live = $(`[data-cube-builder="${CSS.escape(builder.dataset.cubeBuilder)}"]`, els.stage);
      live?.classList.remove("is-loading");
      live?.removeAttribute("aria-busy");
    }
  }

  function artifactEmbedUrl(value) {
    try {
      const url = new URL(value, window.location.href);
      if (
        url.origin === window.location.origin
        && url.pathname.startsWith("/calliope/artifacts/")
      ) {
        url.searchParams.set("embed", "1");
        return `${url.pathname}${url.search}${url.hash}`;
      }
    } catch { /* retain the original artifact URL */ }
    return value;
  }

  function renderArtifact(surface) {
    const url = surface.payload?.display_url || surface.payload?.url;
    if (!url) return `<div class="chart-empty">Artifact URL unavailable</div>`;
    const embedUrl = artifactEmbedUrl(url);
    return `<div class="artifact-frame">
      <iframe src="${escapeHtml(embedUrl)}" title="${escapeHtml(surface.title)}"
        data-artifact-slug="${escapeHtml(surface.artifact_slug || "")}"
        sandbox="allow-scripts allow-forms allow-popups allow-downloads"
        loading="lazy" scrolling="no" referrerpolicy="same-origin"></iframe>
    </div>`;
  }

  function resetArtifactFrameHeights() {
    $$(".artifact-frame[data-auto-height='true']", els.stage).forEach((frame) => {
      frame.style.removeProperty("height");
      delete frame.dataset.autoHeight;
      const iframe = $("iframe", frame);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        iframe?.contentWindow?.postMessage({ type: "calliope.artifact.measure" }, "*");
      }));
    });
  }

  function renderImage(surface) {
    const url = surface.payload?.image_url;
    const baseUrl = surface.payload?.base_image_url;
    const overlayUrl = surface.payload?.overlay_image_url;
    const imageStatus = surface.payload?.image_status;
    const width = Number(surface.payload?.width);
    const height = Number(surface.payload?.height);
    if (url && baseUrl && overlayUrl) {
      const aspect = Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0
        ? ` style="aspect-ratio:${width}/${height}"`
        : "";
      return `<div class="image-body annotated-image" data-markup-visible="true">
        <div class="annotation-stack"${aspect}>
          <img class="annotation-base" src="${escapeHtml(baseUrl)}" alt="${escapeHtml(surface.title)} without markup">
          <img class="annotation-overlay" src="${escapeHtml(overlayUrl)}" alt="">
        </div>
      </div>`;
    }
    return `<div class="image-body">${
      url
        ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(surface.title)}">`
        : `<div class="chart-empty">${
          imageStatus === "expired"
            ? "Capture expired · the artifact version remains available"
            : "Capture unavailable"
        }</div>`
    }</div>`;
  }

  function renderDocument(surface) {
    const payload = surface.payload || {};
    const url = payload.download_url;
    const filename = payload.filename || payload.original_name || surface.title;
    const extension = String(filename).split(".").at(-1)?.toUpperCase() || "FILE";
    return `<div class="document-body"><div class="document-glyph">§</div>
      <div class="document-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</div>
      <div class="document-meta">${escapeHtml(extension)}${
        payload.bytes ? ` · ${escapeHtml(Number(payload.bytes).toLocaleString())} bytes` : ""
      }</div>
      ${url ? `<a href="${escapeHtml(url)}" download="${escapeHtml(filename)}">Download file</a>` : `<span>File is not available from this server</span>`}
    </div>`;
  }

  function renderSelection(surface) {
    const selection = surface.payload?.selection || {};
    const kind = selection.type === "image_region" ? "Image region" : "Artifact object";
    const descriptor = selection.selector
      || selection.text
      || (selection.bounds
        ? `${Math.round(selection.bounds.width || 0)} × ${Math.round(selection.bounds.height || 0)} px`
        : "Exact spatial target");
    return `<div class="selection-body">
      <div class="selection-target"><i></i><div>
        <strong>${escapeHtml(selection.label || surface.title)}</strong>
        <span>${escapeHtml(kind)}</span>
      </div></div>
      <p class="selection-selector" title="${escapeHtml(descriptor)}">${escapeHtml(descriptor)}</p>
    </div>`;
  }

  function surfaceCard(surface) {
    const designProfile = surface.presentation?.design_profile;
    const meta = [
      surface.artifact_version ? `v${surface.artifact_version}` : null,
      relativeTime(surface.created_at),
    ].filter(Boolean).join(" · ");
    const metadata = surface.kind === "query" && isMetadataQuery(surface);
    const body = ({
      query: renderQuery,
      metric: renderMetric,
      cube: renderCube,
      artifact: renderArtifact,
      image: renderImage,
      document: renderDocument,
      selection: renderSelection,
    })[surface.kind]?.(surface) || `<div class="chart-empty">Surface unavailable</div>`;
    const openUrl = surface.kind === "artifact"
      ? surface.payload?.display_url || surface.payload?.url
      : surface.kind === "document" ? surface.payload?.download_url : null;
    const lineage = `<div class="surface-lineage"><i></i><span>${
      surface.parent_surface_id ? "Revision linked to an earlier surface" : "First surface in this lineage"
    }</span></div>`;
    const content = metadata
      ? `<details class="metadata-details">
          <summary>
            <span><b>${escapeHtml(surface.payload?.row_count ?? queryRows(surface).length)}</b> rows · system catalog lookup</span>
            <span class="metadata-toggle"><i class="when-closed">Expand</i><i class="when-open">Collapse</i>⌄</span>
          </summary>
          <div class="surface-body">${body}</div>
          ${lineage}
        </details>`
      : `<div class="surface-body">${body}</div>${lineage}`;
    return `<article class="surface kind-${escapeHtml(surface.kind)} ${metadata ? "metadata-surface" : ""} ${
      state.selectedSurfaceId === surface.id ? "selected" : ""
    }" data-surface-id="${escapeHtml(surface.id)}" aria-current="${
      state.selectedSurfaceId === surface.id ? "true" : "false"
    }">
      <header class="surface-head">
        <span class="surface-kind">${surfaceGlyph(surface.kind)} ${metadata ? "metadata" : escapeHtml(surface.kind)}</span>
        <div class="surface-titles"><h3>${escapeHtml(surface.title)}</h3><p>${escapeHtml(meta)}${
          designProfile
            ? `<span class="style-profile-badge" title="Pinned Design Profile version">${escapeHtml(designProfile.name)} · v${escapeHtml(designProfile.version)}</span>`
            : ""
        }</p></div>
        <div class="surface-tools">
          ${surface.kind === "image" && surface.payload?.overlay_image_url
            ? `<button type="button" data-toggle-markup="${escapeHtml(surface.id)}" aria-pressed="true" title="Hide or show markup">Marks</button>`
            : ""}
          ${surface.kind === "image" && surface.payload?.image_url
            ? `<button type="button" data-markup-surface="${escapeHtml(surface.id)}" title="Select or draw on this image">Markup</button>`
            : ""}
          ${surface.kind === "artifact"
            ? `<button type="button" data-inspect-artifact="${escapeHtml(surface.id)}" aria-pressed="${
              state.inspectingSurfaceId === surface.id ? "true" : "false"
            }" title="Select an object inside this artifact">${
              state.inspectingSurfaceId === surface.id ? "Picking…" : "Select"
            }</button>`
            : ""}
          <button type="button" data-source-turn="${escapeHtml(surface.turn_id)}" title="Jump to source message">↗</button>
          ${openUrl ? `<a href="${escapeHtml(openUrl)}" target="_blank" rel="noopener" title="Open full size">↥</a>` : ""}
        </div>
      </header>
      ${content}
    </article>`;
  }

  function renderStage(initial = false) {
    els.surfaceCount.textContent = `${state.surfaces.length} surface${state.surfaces.length === 1 ? "" : "s"}`;
    els.stageEmpty.hidden = Boolean(state.surfaces.length);
    const turns = [...state.turns].reverse().filter((turn) => surfacesForTurn(turn.id).length);
    els.stage.innerHTML = turns.map((turn) => `
      <section class="stratum" data-stratum-turn="${escapeHtml(turn.id)}">
        <header class="stratum-head">
          <span>Turn ${escapeHtml(turn.ordinal)}</span>
          <button type="button" data-source-turn="${escapeHtml(turn.id)}">“${escapeHtml(turn.user_message)}”</button>
          <span>${escapeHtml(relativeTime(turn.created_at))}</span>
        </header>
        <div class="surface-grid">${surfacesForTurn(turn.id).map(surfaceCard).join("")}</div>
      </section>
    `).join("");
    requestAnimationFrame(initializeCubeBuilders);
    if (initial) {
      els.stageScroll.scrollTop = 0;
      state.stageAtLiveEdge = true;
    }
  }

  function focusSurface(id) {
    const surface = state.surfaces.find((item) => item.id === id);
    if (!surface) return;
    if (state.selectedSurfaceId === id) {
      clearSurfaceSelection();
      return;
    }
    state.selectedSurfaceId = id;
    setMobilePanel();
    renderSelected();
    renderDesignProfileChip();
    $$(".surface.selected").forEach((element) => {
      element.classList.remove("selected");
      element.setAttribute("aria-current", "false");
    });
    const element = $(`.surface[data-surface-id="${CSS.escape(id)}"]`);
    if (element) {
      element.classList.add("selected");
      element.setAttribute("aria-current", "true");
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      element.animate?.([
        { boxShadow: "0 0 0 7px rgba(245,180,70,.22),0 0 70px rgba(245,180,70,.48)" },
        { boxShadow: "0 0 0 6px rgba(245,180,70,.08),0 0 44px rgba(245,180,70,.25)" },
      ], { duration: 900 });
    }
  }

  function clearSurfaceSelection() {
    state.selectedSurfaceId = null;
    renderSelected();
    renderDesignProfileChip();
    $$(".surface.selected").forEach((element) => {
      element.classList.remove("selected");
      element.setAttribute("aria-current", "false");
    });
  }

  function renderSelected() {
    const surface = state.surfaces.find((item) => item.id === state.selectedSurfaceId);
    if (!surface) {
      els.selectedReference.hidden = true;
      els.selectedReference.innerHTML = "";
      return;
    }
    els.selectedReference.hidden = false;
    els.selectedReference.innerHTML = `${surfaceGlyph(surface.kind)} In chat context · <strong>${escapeHtml(surface.title)}</strong>
      <button type="button" data-clear-selection aria-label="Clear reference">×</button>`;
  }

  function spatialSelectionId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `selection-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  function artifactFrame(surfaceId) {
    return $(`.surface[data-surface-id="${CSS.escape(surfaceId)}"] iframe[data-artifact-slug]`);
  }

  function postArtifactInspection(surfaceId, message) {
    const iframe = artifactFrame(surfaceId);
    iframe?.contentWindow?.postMessage(message, "*");
  }

  function setInspectionButton(surfaceId, active) {
    const button = $(`[data-inspect-artifact="${CSS.escape(surfaceId)}"]`);
    if (!button) return;
    button.setAttribute("aria-pressed", String(active));
    button.textContent = active ? "Picking…" : "Select";
  }

  function cancelArtifactInspection(notify = true) {
    const surfaceId = state.inspectingSurfaceId;
    if (!surfaceId) return;
    if (notify) {
      postArtifactInspection(surfaceId, { type: "calliope.artifact.inspect.cancel" });
    }
    setInspectionButton(surfaceId, false);
    state.inspectingSurfaceId = null;
  }

  function startArtifactInspection(surfaceId) {
    const surface = state.surfaces.find((item) => item.id === surfaceId && item.kind === "artifact");
    const iframe = artifactFrame(surfaceId);
    if (!surface || !iframe?.contentWindow) {
      toast("That artifact is not ready for object selection", true);
      return;
    }
    if (state.inspectingSurfaceId === surfaceId) {
      cancelArtifactInspection();
      return;
    }
    cancelArtifactInspection();
    state.inspectingSurfaceId = surfaceId;
    setInspectionButton(surfaceId, true);
    iframe.contentWindow.postMessage({ type: "calliope.artifact.inspect.start" }, "*");
    toast("Move over the artifact and click the object you mean · Esc cancels");
  }

  function matchingCapture(selection) {
    if (selection?.type !== "artifact_element") return null;
    const source = state.surfaces.find((item) => item.id === selection.source_surface_id);
    if (!source?.artifact_slug) return null;
    return state.surfaces.find((item) =>
      item.kind === "image"
      && item.payload?.image_url
      && item.artifact_slug === source.artifact_slug
      && (
        !source.artifact_version
        || !item.artifact_version
        || item.artifact_version === source.artifact_version
      )
    ) || null;
  }

  function renderSpatialSelectionTray() {
    const selections = state.spatialSelections;
    els.spatialSelectionTray.hidden = !selections.length;
    els.spatialSelectionTray.innerHTML = selections.map((selection) => {
      const source = state.surfaces.find((item) => item.id === selection.source_surface_id);
      const capture = matchingCapture(selection);
      const detail = selection.selector
        || selection.text
        || (selection.type === "image_region" ? "selected image region" : "selected object");
      return `<div class="spatial-selection-chip" data-spatial-selection="${escapeHtml(selection.selection_id)}">
        <i aria-hidden="true"></i>
        <div class="spatial-selection-copy">
          <strong>${escapeHtml(selection.label || "Selected target")}</strong>
          <span title="${escapeHtml(detail)}">${escapeHtml(source?.title || "Surface")} · ${escapeHtml(detail)}</span>
        </div>
        <div class="spatial-selection-actions">
          ${capture ? `<button type="button" data-draw-selection="${escapeHtml(selection.selection_id)}">Draw too</button>` : ""}
          <button type="button" data-remove-spatial-selection="${escapeHtml(selection.selection_id)}" aria-label="Remove target">×</button>
        </div>
      </div>`;
    }).join("");
  }

  function removeSpatialSelection(selectionId) {
    const selection = state.spatialSelections.find((item) => item.selection_id === selectionId);
    if (selection?.type === "artifact_element") {
      postArtifactInspection(selection.source_surface_id, {
        type: "calliope.artifact.inspect.clear",
        selection_id: selection.selection_id,
      });
    }
    state.spatialSelections = state.spatialSelections.filter(
      (item) => item.selection_id !== selectionId,
    );
    renderSpatialSelectionTray();
  }

  function clearSpatialSelections() {
    cancelArtifactInspection();
    state.spatialSelections.forEach((selection) => {
      if (selection.type === "artifact_element") {
        postArtifactInspection(selection.source_surface_id, {
          type: "calliope.artifact.inspect.clear",
          selection_id: selection.selection_id,
        });
      }
    });
    state.spatialSelections = [];
    renderSpatialSelectionTray();
  }

  function acceptArtifactSelection(surface, target) {
    if (!surface || state.spatialSelections.length >= 8) {
      toast("A message can include at most eight spatial targets", true);
      if (target?.selection_id) {
        postArtifactInspection(surface?.id, {
          type: "calliope.artifact.inspect.clear",
          selection_id: target.selection_id,
        });
      }
      return;
    }
    const selection = {
      selection_id: String(target?.selection_id || spatialSelectionId()),
      source_surface_id: surface.id,
      type: "artifact_element",
      label: String(target?.label || target?.text || target?.selector || "Selected object"),
      selector: String(target?.selector || ""),
      tag: String(target?.tag || ""),
      role: String(target?.role || ""),
      text: String(target?.text || ""),
      data: target?.data && typeof target.data === "object" ? target.data : {},
      bounds: target?.bounds && typeof target.bounds === "object" ? target.bounds : {},
      viewport: target?.viewport && typeof target.viewport === "object" ? target.viewport : {},
      click: target?.click && typeof target.click === "object" ? target.click : {},
      table: target?.table && typeof target.table === "object" ? target.table : null,
    };
    state.spatialSelections.push(selection);
    state.selectedSurfaceId = surface.id;
    renderSelected();
    renderDesignProfileChip();
    renderSpatialSelectionTray();
    $$(".surface.selected").forEach((element) => {
      element.classList.remove("selected");
      element.setAttribute("aria-current", "false");
    });
    const card = $(`.surface[data-surface-id="${CSS.escape(surface.id)}"]`);
    card?.classList.add("selected");
    card?.setAttribute("aria-current", "true");
    toast(`Target added · ${selection.label.slice(0, 80)}`);
  }

  function jumpToTurn(id) {
    setMobilePanel("chat");
    const element = $(`.message[data-turn-id="${CSS.escape(id)}"]`);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      element.animate?.([
        { background: "rgba(245,180,70,.13)" },
        { background: "transparent" },
      ], { duration: 1100 });
    }
  }

  function drawMarkupStroke(ctx, stroke) {
    if (!stroke?.points?.length) return;
    const first = stroke.points[0];
    const last = stroke.points.at(-1);
    if (stroke.tool === "select") {
      const x = Math.min(first.x, last.x);
      const y = Math.min(first.y, last.y);
      const width = Math.abs(last.x - first.x);
      const height = Math.abs(last.y - first.y);
      ctx.save();
      ctx.strokeStyle = stroke.color || "#68c7b2";
      ctx.fillStyle = `${stroke.color || "#68c7b2"}20`;
      ctx.lineWidth = Math.max(2, stroke.width || 3);
      ctx.setLineDash([Math.max(7, ctx.lineWidth * 2), Math.max(4, ctx.lineWidth)]);
      ctx.fillRect(x, y, width, height);
      ctx.strokeRect(x, y, width, height);
      ctx.setLineDash([]);
      ctx.fillStyle = stroke.color || "#68c7b2";
      ctx.font = `${Math.max(13, (stroke.width || 3) * 3)}px ui-monospace, monospace`;
      ctx.fillText("TARGET", x + 6, Math.max(16, y - 6));
      ctx.restore();
      return;
    }
    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    if (stroke.tool === "pen") {
      ctx.moveTo(first.x, first.y);
      stroke.points.slice(1).forEach((point) => ctx.lineTo(point.x, point.y));
    } else if (stroke.tool === "rect") {
      ctx.rect(
        Math.min(first.x, last.x),
        Math.min(first.y, last.y),
        Math.abs(last.x - first.x),
        Math.abs(last.y - first.y),
      );
    } else {
      ctx.moveTo(first.x, first.y);
      ctx.lineTo(last.x, last.y);
      const angle = Math.atan2(last.y - first.y, last.x - first.x);
      const head = Math.max(12, stroke.width * 3.5);
      [-1, 1].forEach((side) => {
        ctx.moveTo(last.x, last.y);
        ctx.lineTo(
          last.x - head * Math.cos(angle + side * 0.45),
          last.y - head * Math.sin(angle + side * 0.45),
        );
      });
    }
    ctx.stroke();
  }

  function paintMarkupCanvas() {
    const markup = state.markup;
    const canvas = els.markupCanvas;
    if (!markup.ready || !markup.image || !canvas.width || !canvas.height) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(markup.image, 0, 0, canvas.width, canvas.height);
    markup.strokes.forEach((stroke) => drawMarkupStroke(ctx, stroke));
    if (markup.liveStroke) drawMarkupStroke(ctx, markup.liveStroke);
  }

  function syncMarkupControls() {
    const markup = state.markup;
    $$("[data-markup-tool]", els.markupToolbar).forEach((button) => {
      button.classList.toggle("active", button.dataset.markupTool === markup.tool);
    });
    $$("[data-markup-color]", els.markupToolbar).forEach((button) => {
      button.classList.toggle("active", button.dataset.markupColor === markup.color);
    });
    $$("[data-markup-width]", els.markupToolbar).forEach((button) => {
      button.classList.toggle("active", Number(button.dataset.markupWidth) === markup.width);
    });
    els.markupCanvas.dataset.tool = markup.tool;
    els.markupUndo.disabled = !markup.strokes.length;
    els.markupClear.disabled = !markup.strokes.length;
  }

  function resetMarkup() {
    state.markup.surface = null;
    state.markup.image = null;
    state.markup.strokes = [];
    state.markup.liveStroke = null;
    state.markup.pendingSelection = null;
    state.markup.tool = "select";
    state.markup.ready = false;
    els.markupCanvas.classList.remove("ready");
    els.markupCanvas.width = 0;
    els.markupCanvas.height = 0;
    els.markupLoading.hidden = false;
    els.markupAttach.disabled = true;
    syncMarkupControls();
  }

  function closeMarkup() {
    if (els.markupDialog.open) els.markupDialog.close();
    resetMarkup();
  }

  function openMarkup(surfaceId, pendingSelection = null) {
    const surface = state.surfaces.find((item) => item.id === surfaceId);
    const url = surface?.payload?.image_url;
    if (!surface || !url) {
      toast("That image is not available for markup", true);
      return;
    }
    resetMarkup();
    state.markup.surface = surface;
    state.markup.pendingSelection = pendingSelection;
    els.markupTitle.textContent = `Select or mark up · ${surface.title}`;
    els.markupDialog.showModal();
    const image = new Image();
    image.onload = () => {
      if (state.markup.surface?.id !== surface.id) return;
      const maxSide = 2200;
      const scale = Math.min(1, maxSide / Math.max(image.naturalWidth, image.naturalHeight));
      els.markupCanvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      els.markupCanvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      state.markup.image = image;
      state.markup.ready = true;
      els.markupLoading.hidden = true;
      els.markupCanvas.classList.add("ready");
      els.markupAttach.disabled = false;
      const bounds = pendingSelection?.bounds;
      const viewport = pendingSelection?.viewport;
      if (
        bounds && viewport
        && Number(viewport.width) > 0 && Number(viewport.height) > 0
        && Number(bounds.width) > 0 && Number(bounds.height) > 0
      ) {
        const x1 = Number(bounds.x) / Number(viewport.width) * els.markupCanvas.width;
        const y1 = Number(bounds.y) / Number(viewport.height) * els.markupCanvas.height;
        const x2 = (Number(bounds.x) + Number(bounds.width)) / Number(viewport.width) * els.markupCanvas.width;
        const y2 = (Number(bounds.y) + Number(bounds.height)) / Number(viewport.height) * els.markupCanvas.height;
        if (
          [x1, y1, x2, y2].every(Number.isFinite)
          && x1 < els.markupCanvas.width
          && y1 < els.markupCanvas.height
        ) {
          state.markup.strokes.push({
            tool: "select",
            color: "#68c7b2",
            width: 4,
            points: [
              { x: Math.max(0, x1), y: Math.max(0, y1) },
              {
                x: Math.min(els.markupCanvas.width, x2),
                y: Math.min(els.markupCanvas.height, y2),
              },
            ],
          });
        }
      }
      syncMarkupControls();
      paintMarkupCanvas();
    };
    image.onerror = () => {
      closeMarkup();
      toast("Calliope could not open that image", true);
    };
    image.src = url;
  }

  function markupPoint(event) {
    const rect = els.markupCanvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) / rect.width * els.markupCanvas.width,
      y: (event.clientY - rect.top) / rect.height * els.markupCanvas.height,
    };
  }

  function markupPointerDown(event) {
    if (!state.markup.ready || event.button !== 0) return;
    event.preventDefault();
    try { els.markupCanvas.setPointerCapture(event.pointerId); } catch { /* drawing still works */ }
    state.markup.liveStroke = {
      tool: state.markup.tool,
      color: state.markup.color,
      width: state.markup.width,
      points: [markupPoint(event)],
    };
    paintMarkupCanvas();
  }

  function markupPointerMove(event) {
    const stroke = state.markup.liveStroke;
    if (!stroke) return;
    const point = markupPoint(event);
    if (stroke.tool === "pen") stroke.points.push(point);
    else stroke.points = [stroke.points[0], point];
    paintMarkupCanvas();
  }

  function markupPointerUp() {
    const stroke = state.markup.liveStroke;
    state.markup.liveStroke = null;
    if (stroke && stroke.points.length > 1) state.markup.strokes.push(stroke);
    paintMarkupCanvas();
    syncMarkupControls();
  }

  function dataUrlBytes(dataUrl) {
    const encoded = String(dataUrl || "").split(",", 2)[1] || "";
    return Math.floor(encoded.length * 3 / 4);
  }

  function attachMarkup() {
    const markup = state.markup;
    const source = markup.surface;
    const canvas = els.markupCanvas;
    if (!markup.ready || !source || state.attachments.length >= 4) {
      if (state.attachments.length >= 4) toast("A message can include at most four images", true);
      return;
    }
    paintMarkupCanvas();
    const composite = canvas.toDataURL("image/webp", 0.88);
    const overlay = document.createElement("canvas");
    overlay.width = canvas.width;
    overlay.height = canvas.height;
    const overlayCtx = overlay.getContext("2d");
    if (!overlayCtx) {
      toast("This browser cannot prepare the markup overlay", true);
      return;
    }
    markup.strokes.forEach((stroke) => drawMarkupStroke(overlayCtx, stroke));
    const overlayData = overlay.toDataURL("image/png");
    const bytes = dataUrlBytes(composite) + dataUrlBytes(overlayData);
    if (state.config?.max_image_bytes && bytes > state.config.max_image_bytes) {
      toast("That annotated image is too large to attach", true);
      return;
    }
    const extension = composite.startsWith("data:image/webp;") ? "webp" : "png";
    const stem = String(source.title || "image").replace(/\.[a-z0-9]+$/i, "").slice(0, 120);
    const regions = markup.strokes
      .filter((stroke) => stroke.tool === "select" && stroke.points.length > 1)
      .map((stroke, index) => {
        const first = stroke.points[0];
        const last = stroke.points.at(-1);
        return {
          selection_id: spatialSelectionId(),
          source_surface_id: source.id,
          type: "image_region",
          label: `Selected image region ${index + 1}`,
          bounds: {
            x: Math.round(Math.min(first.x, last.x)),
            y: Math.round(Math.min(first.y, last.y)),
            width: Math.round(Math.abs(last.x - first.x)),
            height: Math.round(Math.abs(last.y - first.y)),
          },
          viewport: { width: canvas.width, height: canvas.height },
        };
      })
      .filter((selection) => selection.bounds.width > 2 && selection.bounds.height > 2);
    state.attachments.push({
      name: `${stem} · annotated.${extension}`,
      data_url: composite,
      width: canvas.width,
      height: canvas.height,
      annotation: {
        source_surface_id: source.id,
        overlay_data_url: overlayData,
        width: canvas.width,
        height: canvas.height,
        selections: regions.map((selection) => ({
          selection_id: selection.selection_id,
          label: selection.label,
          bounds: selection.bounds,
        })),
      },
    });
    const availableTargets = Math.max(0, 8 - state.spatialSelections.length);
    state.spatialSelections.push(...regions.slice(0, availableTargets));
    state.selectedSurfaceId = source.id;
    renderSelected();
    $$(".surface.selected").forEach((element) => element.classList.remove("selected"));
    $(`.surface[data-surface-id="${CSS.escape(source.id)}"]`)?.classList.add("selected");
    renderAttachmentTray();
    renderSpatialSelectionTray();
    closeMarkup();
    setMobilePanel("chat");
    els.input.focus();
    toast(regions.length ? "Spatial target and marked image added to the next message" : "Annotated image added to the next message");
  }

  async function readFiles(files) {
    const before = state.attachments.length;
    const accepted = [...files].slice(0, Math.max(0, 4 - state.attachments.length));
    for (const file of accepted) {
      if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) {
        toast(`${file.name} is not a supported image`, true);
        continue;
      }
      if (state.config?.max_image_bytes && file.size > state.config.max_image_bytes) {
        toast(`${file.name} is too large`, true);
        continue;
      }
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
      }[file.type] || "png";
      const name = file.name || `Pasted image ${state.attachments.length + 1}.${extension}`;
      state.attachments.push({ name, data_url: dataUrl });
    }
    renderAttachmentTray();
    return state.attachments.length - before;
  }

  function pastedImageFiles(event) {
    const clipboard = event.clipboardData;
    if (!clipboard) return [];
    const direct = [...(clipboard.files || [])].filter((file) => file.type.startsWith("image/"));
    if (direct.length) return direct;
    return [...(clipboard.items || [])]
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter(Boolean);
  }

  function pasteImages(event) {
    const images = pastedImageFiles(event);
    if (!images.length) return;
    event.preventDefault();
    readFiles(images)
      .then((added) => {
        if (!added) {
          toast("A message can include at most four supported images", true);
        } else {
          toast(added === 1 ? "Pasted image attached" : `${added} pasted images attached`);
        }
      })
      .catch((error) => toast(error.message, true));
  }

  function renderAttachmentTray() {
    els.attachmentTray.hidden = !state.attachments.length;
    els.attachmentTray.innerHTML = state.attachments.map((attachment, index) => `
      <div class="attachment-preview">
        <img src="${escapeHtml(attachment.data_url)}" alt="${escapeHtml(attachment.name)}">
        ${attachment.annotation ? `<span class="annotation-badge">${
          attachment.annotation.selections?.length ? "spatial" : "markup"
        }</span>` : ""}
        <button type="button" data-remove-attachment="${index}" aria-label="Remove ${escapeHtml(attachment.name)}">×</button>
      </div>`).join("");
  }

  function resizeComposer() {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(180, els.input.scrollHeight)}px`;
  }

  function optimisticTurn(message, hasSpatialSelection = false) {
    const maxOrdinal = Math.max(0, ...state.turns.map((turn) => Number(turn.ordinal || 0)));
    const turn = {
      id: `pending-${Date.now()}`,
      ordinal: maxOrdinal + 1,
      user_message: message || (hasSpatialSelection ? "[Object selection]" : "[Image]"),
      assistant_message: "",
      attachments: state.attachments.map((attachment) => ({
        name: attachment.name,
        url: attachment.data_url,
      })),
      status: "running",
      created_at: new Date().toISOString(),
    };
    state.turns.push(turn);
    renderChat();
    requestAnimationFrame(() => { els.messages.scrollTop = els.messages.scrollHeight; });
    return turn;
  }

  async function parseEventStream(response, handler) {
    if (!response.ok) {
      let detail = "";
      try { detail = (await response.json())?.error?.message; } catch { detail = await response.text(); }
      throw new Error(detail || `Turn failed (${response.status})`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let event = "message";
        const data = [];
        block.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
        });
        if (data.length) {
          let parsed;
          try { parsed = JSON.parse(data.join("\n")); } catch { parsed = { text: data.join("\n") }; }
          await handler(event, parsed);
        }
      }
      if (done) break;
    }
  }

  async function sendTurn() {
    if (!state.current || state.busy) return;
    const message = els.input.value.trim();
    if (!message && !state.attachments.length && !state.spatialSelections.length) return;
    const outgoingAttachments = [...state.attachments];
    const outgoingSpatialSelections = state.spatialSelections.map((selection) => ({ ...selection }));
    const outgoingDesignProfileVersionId = state.nextTurnDesignProfileVersionId;
    const pending = optimisticTurn(message, Boolean(outgoingSpatialSelections.length));
    els.input.value = "";
    state.attachments = [];
    clearSpatialSelections();
    state.nextTurnDesignProfileVersionId = null;
    renderAttachmentTray();
    renderDesignProfileChip();
    resizeComposer();
    state.busy = true;
    els.send.disabled = true;
    els.input.disabled = true;
    setStatus("working", "working");
    els.toolActivity.hidden = false;
    els.toolActivity.innerHTML = "<strong>Calliope is reading the notebook…</strong>";

    try {
      const response = await fetch(`/api/calliope/sessions/${state.current.id}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          attachments: outgoingAttachments,
          spatial_selections: outgoingSpatialSelections,
          selected_surface_id: state.selectedSurfaceId,
          ...(outgoingDesignProfileVersionId
            ? { design_profile_version_id: outgoingDesignProfileVersionId }
            : {}),
        }),
      });
      await parseEventStream(response, async (event, data) => {
        if (event === "calliope.turn.started") {
          pending.id = data.turn_id;
          pending.ordinal = data.ordinal;
          pending.attachments = data.attachments || pending.attachments;
          renderChat();
          els.messages.scrollTop = els.messages.scrollHeight;
        } else if (event === "assistant.delta") {
          pending.assistant_message += data.delta || "";
          const body = $(`[data-assistant-turn-id="${CSS.escape(pending.id)}"] .message-body`);
          if (body) body.innerHTML = safeMarkdown(pending.assistant_message);
          els.messages.scrollTop = els.messages.scrollHeight;
        } else if (event === "assistant.completed") {
          pending.assistant_message = data.content || pending.assistant_message;
          renderChat();
          els.messages.scrollTop = els.messages.scrollHeight;
        } else if (event === "calliope.visual_check") {
          pending.assistant_message = "";
          pending.status = "running";
          renderChat();
          els.toolActivity.hidden = false;
          els.toolActivity.innerHTML = `<strong>Calliope is reviewing the rendered image</strong> · visual check ${
            escapeHtml(data.number || 1)
          }/${escapeHtml(data.budget || 2)}`;
          els.messages.scrollTop = els.messages.scrollHeight;
        } else if (event === "tool.started") {
          const name = friendlyTool(data.tool_name);
          els.toolActivity.innerHTML = `<strong>${escapeHtml(name)}</strong> · ${escapeHtml(data.preview || "working…")}`;
        } else if (event === "tool.completed") {
          els.toolActivity.innerHTML = `<strong>${escapeHtml(friendlyTool(data.tool_name))}</strong> · placed result`;
        } else if (event === "calliope.surfaces") {
          const incoming = data.surfaces || [];
          state.surfaces = [...incoming, ...state.surfaces.filter((surface) =>
            !incoming.some((next) => next.id === surface.id)
          )];
          if (!state.stageAtLiveEdge && incoming.length) {
            state.newSurfaceCount += incoming.length;
            els.newSurfaces.hidden = false;
            els.newSurfaces.textContent = `${state.newSurfaceCount} new surface${state.newSurfaceCount === 1 ? "" : "s"} ↑`;
          }
          renderStage(state.stageAtLiveEdge);
          renderChat();
        } else if (event === "calliope.turn.completed") {
          pending.status = "complete";
          pending.assistant_message = data.assistant_message || pending.assistant_message;
          renderChat();
          els.messages.scrollTop = els.messages.scrollHeight;
        } else if (event === "calliope.error" || event === "error") {
          throw new Error(data.message || "Calliope could not complete the turn");
        }
      });
      await loadSessions(state.current.id);
    } catch (error) {
      pending.status = "failed";
      pending.error = error.message;
      if (outgoingDesignProfileVersionId && !state.nextTurnDesignProfileVersionId) {
        state.nextTurnDesignProfileVersionId = outgoingDesignProfileVersionId;
        renderDesignProfileChip();
      }
      renderChat();
      toast(error.message, true);
    } finally {
      state.busy = false;
      els.input.disabled = false;
      els.send.disabled = false;
      els.toolActivity.hidden = true;
      setStatus(state.config?.healthy ? "ready" : "unavailable", state.config?.healthy ? "" : "offline");
      els.input.focus();
    }
  }

  function friendlyTool(name) {
    const raw = String(name || "warehouse tool").split("_");
    const known = ["run_sql_multi", "run_sql", "create_live_app", "update_live_app", "publish_dashboard", "update_dashboard", "capture_live_app", "render_pdf", "metric_history", "describe_cube", "pivot", "metric"];
    const value = String(name || "");
    const found = known.find((tool) => value === tool || value.endsWith(`__${tool}`));
    return (found || raw.slice(-2).join("_")).replaceAll("_", " ");
  }

  function setupEvents() {
    els.styleOpen.addEventListener("click", () => {
      openDesignProfiles().catch((error) => toast(error.message, true));
    });
    els.styleClose.addEventListener("click", () => els.styleDialog.close());
    els.styleNew.addEventListener("click", showNewDesignProfile);
    els.styleList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-design-profile]");
      if (!button) return;
      selectDesignProfile(button.dataset.designProfile).catch((error) => toast(error.message, true));
    });
    els.styleImages.addEventListener("change", () => {
      readDesignSourceImages(els.styleImages.files).catch((error) => toast(error.message, true));
      els.styleImages.value = "";
    });
    els.styleSourceStrip.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-design-source]");
      if (!button) return;
      state.designSourceImages.splice(Number(button.dataset.removeDesignSource), 1);
      renderDesignSourceStrip();
    });
    els.styleUseSelected.addEventListener("click", () => {
      if (!eligibleSelectedDesignSource()) return;
      state.useSelectedAsDesignSource = !state.useSelectedAsDesignSource;
      renderDesignSourceStrip();
    });
    els.styleGenerate.addEventListener("click", () => {
      generateDesignProfile().catch((error) => toast(error.message, true));
    });
    els.styleVersion.addEventListener("change", () => {
      state.designProfileVersionId = els.styleVersion.value;
      renderDesignEditor();
    });
    els.styleSaveVersion.addEventListener("click", () => {
      saveDesignProfileVersion().catch((error) => toast(error.message, true));
    });
    els.styleFork.addEventListener("click", () => {
      forkDesignProfile().catch((error) => toast(error.message, true));
    });
    els.styleArchive.addEventListener("click", () => {
      archiveDesignProfile().catch((error) => toast(error.message, true));
    });
    els.styleUseOnce.addEventListener("click", () => {
      const selected = selectedDesignVersion();
      if (!selected || !state.current) return;
      state.nextTurnDesignProfileVersionId = selected.version.id;
      renderDesignEditor();
      renderDesignProfileChip();
      els.styleDialog.close();
      els.input.focus();
      toast(`${selected.profile.name} will guide the next turn`);
    });
    els.styleUseSession.addEventListener("click", () => {
      const selected = selectedDesignVersion();
      if (!selected || !state.current) return;
      applyDesignProfileToSession(selected.version.id)
        .then(() => {
          els.styleDialog.close();
          els.input.focus();
          toast(`${selected.profile.name} is now the session Design Profile`);
        })
        .catch((error) => toast(error.message, true));
    });
    els.designProfileChip.addEventListener("click", (event) => {
      const clear = event.target.closest("[data-clear-design-profile]");
      if (clear) {
        clearComposerDesignProfile(clear.dataset.clearDesignProfile)
          .catch((error) => toast(error.message, true));
        return;
      }
      const target = event.target.closest("[data-open-design-profile]");
      if (target) {
        openDesignProfiles(target.dataset.openDesignProfile)
          .catch((error) => toast(error.message, true));
      }
    });
    els.chatResizer.addEventListener("pointerdown", beginChatResize);
    els.chatResizer.addEventListener("pointermove", moveChatResize);
    els.chatResizer.addEventListener("pointerup", endChatResize);
    els.chatResizer.addEventListener("pointercancel", endChatResize);
    els.chatResizer.addEventListener("dblclick", () => setChatWidth(CHAT_DEFAULT_WIDTH, true));
    els.chatResizer.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Home") {
        setChatWidth(CHAT_DEFAULT_WIDTH, true);
      } else {
        setChatWidth((state.chatWidth || CHAT_DEFAULT_WIDTH) + (event.key === "ArrowLeft" ? 24 : -24), true);
      }
      clearTimeout(state.artifactResizeTimer);
      state.artifactResizeTimer = setTimeout(resetArtifactFrameHeights, 120);
    });
    els.mobileSessions.addEventListener("click", () => {
      setMobilePanel(document.body.classList.contains("mobile-sessions-open") ? null : "sessions");
    });
    els.mobileChat.addEventListener("click", () => {
      setMobilePanel(document.body.classList.contains("mobile-chat-open") ? null : "chat");
    });
    els.mobileShade.addEventListener("click", () => setMobilePanel());
    window.addEventListener("resize", () => {
      if (!window.matchMedia("(max-width: 880px)").matches) setMobilePanel();
      if (state.chatWidth != null) setChatWidth(state.chatWidth, false);
      clearTimeout(state.artifactResizeTimer);
      state.artifactResizeTimer = setTimeout(resetArtifactFrameHeights, 120);
    });
    els.newSession.addEventListener("click", () => {
      els.dialog.showModal();
      requestAnimationFrame(() => els.newSessionTitle.focus());
    });
    els.newSessionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const title = els.newSessionTitle.value.trim() || "New inquiry";
      try { await createSession(title); } catch (error) { toast(error.message, true); }
    });
    els.sessionSearch.addEventListener("input", renderSessions);
    els.sessionList.addEventListener("click", (event) => {
      const card = event.target.closest("[data-session-id]");
      if (card) selectSession(card.dataset.sessionId).catch((error) => toast(error.message, true));
    });
    els.sessionTitle.addEventListener("click", () => renameSession().catch((error) => toast(error.message, true)));
    els.archiveSession.addEventListener("click", () => archiveSession().catch((error) => toast(error.message, true)));
    els.composer.addEventListener("submit", (event) => {
      event.preventDefault();
      sendTurn();
    });
    els.input.addEventListener("input", resizeComposer);
    els.input.addEventListener("paste", pasteImages);
    els.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendTurn();
      }
    });
    els.imageInput.addEventListener("change", () => {
      readFiles(els.imageInput.files).catch((error) => toast(error.message, true));
      els.imageInput.value = "";
    });
    els.attachmentTray.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-attachment]");
      if (!button) return;
      state.attachments.splice(Number(button.dataset.removeAttachment), 1);
      renderAttachmentTray();
    });
    els.spatialSelectionTray.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-remove-spatial-selection]");
      if (remove) {
        removeSpatialSelection(remove.dataset.removeSpatialSelection);
        return;
      }
      const draw = event.target.closest("[data-draw-selection]");
      if (!draw) return;
      const selection = state.spatialSelections.find(
        (item) => item.selection_id === draw.dataset.drawSelection,
      );
      const capture = matchingCapture(selection);
      if (!capture) {
        toast("No matching capture is available for markup yet", true);
        return;
      }
      openMarkup(capture.id, selection);
    });
    els.markupToolbar.addEventListener("click", (event) => {
      const tool = event.target.closest("[data-markup-tool]");
      const color = event.target.closest("[data-markup-color]");
      const width = event.target.closest("[data-markup-width]");
      if (tool) state.markup.tool = tool.dataset.markupTool;
      if (color) state.markup.color = color.dataset.markupColor;
      if (width) state.markup.width = Number(width.dataset.markupWidth);
      syncMarkupControls();
    });
    els.markupUndo.addEventListener("click", () => {
      state.markup.strokes.pop();
      paintMarkupCanvas();
      syncMarkupControls();
    });
    els.markupClear.addEventListener("click", () => {
      state.markup.strokes = [];
      state.markup.liveStroke = null;
      paintMarkupCanvas();
      syncMarkupControls();
    });
    els.markupCanvas.addEventListener("pointerdown", markupPointerDown);
    els.markupCanvas.addEventListener("pointermove", markupPointerMove);
    els.markupCanvas.addEventListener("pointerup", markupPointerUp);
    els.markupCanvas.addEventListener("pointercancel", markupPointerUp);
    els.markupClose.addEventListener("click", closeMarkup);
    els.markupCancel.addEventListener("click", closeMarkup);
    els.markupAttach.addEventListener("click", attachMarkup);
    els.markupDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeMarkup();
    });
    window.addEventListener("keydown", (event) => {
      if (!els.markupDialog.open || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "z") return;
      event.preventDefault();
      state.markup.strokes.pop();
      paintMarkupCanvas();
      syncMarkupControls();
    });
    els.selectedReference.addEventListener("click", (event) => {
      if (!event.target.closest("[data-clear-selection]")) return;
      clearSurfaceSelection();
    });
    els.messages.addEventListener("click", (event) => {
      const button = event.target.closest("[data-focus-surface]");
      if (button) focusSurface(button.dataset.focusSurface);
    });
    els.stage.addEventListener("click", (event) => {
      const inspect = event.target.closest("[data-inspect-artifact]");
      if (inspect) {
        startArtifactInspection(inspect.dataset.inspectArtifact);
        return;
      }
      const markup = event.target.closest("[data-markup-surface]");
      if (markup) {
        openMarkup(markup.dataset.markupSurface);
        return;
      }
      const toggleMarkup = event.target.closest("[data-toggle-markup]");
      if (toggleMarkup) {
        const card = toggleMarkup.closest(".surface");
        const overlay = $(".annotation-overlay", card);
        const visible = toggleMarkup.getAttribute("aria-pressed") !== "true";
        toggleMarkup.setAttribute("aria-pressed", String(visible));
        toggleMarkup.title = visible ? "Hide markup" : "Show markup";
        if (overlay) overlay.hidden = !visible;
        $(".annotated-image", card)?.setAttribute("data-markup-visible", String(visible));
        return;
      }
      const tab = event.target.closest("[data-view]");
      if (tab) {
        const surface = tab.closest(".surface");
        $$(".surface-tabs button", surface).forEach((button) => button.classList.toggle("active", button === tab));
        $$("[data-query-view]", surface).forEach((view) => { view.hidden = view.dataset.queryView !== tab.dataset.view; });
        return;
      }
      const removeCubeDimension = event.target.closest("[data-cube-remove-field]");
      if (removeCubeDimension) {
        removeCubeField(removeCubeDimension);
        return;
      }
      const removeMeasure = event.target.closest("[data-cube-remove-measure]");
      if (removeMeasure) {
        removeCubeMeasure(removeMeasure);
        return;
      }
      const addCount = event.target.closest("[data-cube-add-count]");
      if (addCount) {
        addCubeRowCount(addCount);
        return;
      }
      const cubeField = event.target.closest("[data-cube-field]");
      if (cubeField) {
        selectCubeField(cubeField);
        return;
      }
      const heat = event.target.closest("[data-cube-heat]");
      if (heat) {
        const shell = heat.closest("[data-cube]");
        const active = heat.getAttribute("aria-pressed") !== "true";
        heat.setAttribute("aria-pressed", String(active));
        shell?.classList.toggle("heat-on", active);
        return;
      }
      const cubeColumn = event.target.closest("[data-cube-column]");
      if (cubeColumn) {
        const shell = cubeColumn.closest("[data-cube]");
        const select = $("[data-cube-sort]", shell);
        if (select) select.value = `cell:${cubeColumn.dataset.cubeColumn}`;
        applyCubeView(shell);
        return;
      }
      const source = event.target.closest("[data-source-turn]");
      if (source) {
        jumpToTurn(source.dataset.sourceTurn);
        return;
      }
      const card = event.target.closest("[data-surface-id]");
      if (card && !event.target.closest("a,button,summary,input,select,label")) focusSurface(card.dataset.surfaceId);
    });
    els.stage.addEventListener("input", (event) => {
      if (!event.target.matches("[data-cube-search]")) return;
      applyCubeView(event.target.closest("[data-cube]"));
    });
    els.stage.addEventListener("change", (event) => {
      if (event.target.matches("[data-cube-measure-aggregate]")) {
        const builder = event.target.closest("[data-cube-builder]");
        const { config } = cubeBuilderContext(builder);
        const field = event.target.dataset.cubeMeasureAggregate;
        const measure = config?.measures.find((item) => item.field === field);
        if (measure) measure.aggregate = event.target.value;
        refreshCubeConfiguration(builder);
        scheduleCubeBuilder(builder);
        return;
      }
      if (event.target.matches("[data-cube-sort]")) {
        applyCubeView(event.target.closest("[data-cube]"));
      }
    });
    els.stageScroll.addEventListener("scroll", () => {
      state.stageAtLiveEdge = els.stageScroll.scrollTop < 90;
      if (state.stageAtLiveEdge) {
        state.newSurfaceCount = 0;
        els.newSurfaces.hidden = true;
      }
    }, { passive: true });
    els.newSurfaces.addEventListener("click", () => {
      els.stageScroll.scrollTo({ top: 0, behavior: "smooth" });
      state.newSurfaceCount = 0;
      els.newSurfaces.hidden = true;
    });
    window.addEventListener("message", async (event) => {
      const data = event.data;
      if (!data) return;
      const iframe = $$("iframe[data-artifact-slug]").find((frame) => frame.contentWindow === event.source);
      if (!iframe) return;
      const surfaceId = iframe.closest("[data-surface-id]")?.dataset.surfaceId;
      const surface = state.surfaces.find((item) => item.id === surfaceId);
      if (data.type === "calliope.artifact.inspect.selected") {
        if (!surface || state.inspectingSurfaceId !== surface.id) return;
        cancelArtifactInspection(false);
        acceptArtifactSelection(surface, data.target);
        return;
      }
      if (data.type === "calliope.artifact.inspect.cancelled") {
        if (state.inspectingSurfaceId === surfaceId) cancelArtifactInspection(false);
        return;
      }
      if (data.type === "calliope.artifact.resize") {
        const height = Math.ceil(Number(data.height));
        if (!Number.isFinite(height) || height < 1) return;
        const frame = iframe.closest(".artifact-frame");
        if (!frame) return;
        frame.style.height = `${Math.max(280, height)}px`;
        frame.dataset.autoHeight = "true";
        return;
      }
      if (data.type !== "calliope.query" || !data.id) return;
      const slug = iframe.dataset.artifactSlug;
      try {
        let result;
        if (data.kind === "multi") {
          const entries = Object.entries(data.queries || {});
          if (!entries.length || entries.length > 24) throw new Error("Invalid query batch");
          const settled = await Promise.all(entries.map(async ([name, sql]) => [
            name,
            await api(`/api/d/${encodeURIComponent(slug)}/q`, {
              method: "POST",
              body: JSON.stringify({ sql, as_of: data.opts?.as_of }),
            }),
          ]));
          result = { results: Object.fromEntries(settled) };
        } else {
          result = await api(`/api/d/${encodeURIComponent(slug)}/q`, {
            method: "POST",
            body: JSON.stringify({ sql: data.sql, as_of: data.opts?.as_of }),
          });
        }
        event.source.postMessage({ type: "calliope.query.result", id: data.id, result }, "*");
      } catch (error) {
        event.source.postMessage({ type: "calliope.query.result", id: data.id, error: error.message }, "*");
      }
    });
    document.addEventListener("dragover", (event) => {
      if ([...event.dataTransfer.types].includes("Files")) event.preventDefault();
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) updateCalliopeAvatar();
    });
    document.addEventListener("drop", (event) => {
      if (!event.dataTransfer.files.length) return;
      event.preventDefault();
      readFiles(event.dataTransfer.files).catch((error) => toast(error.message, true));
    });
  }

  async function init() {
    scheduleAvatarClock();
    restoreChatWidth();
    setupEvents();
    try {
      await loadConfig();
      await loadDesignProfiles();
      await loadSessions();
      if (!state.sessions.length) {
        els.dialog.showModal();
        requestAnimationFrame(() => els.newSessionTitle.focus());
      }
    } catch (error) {
      toast(error.message, true);
      setStatus("unavailable", "offline");
    }
  }

  init();
})();
