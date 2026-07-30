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
    imageInput: $("#image-input"),
    attachmentTray: $("#attachment-tray"),
    selectedReference: $("#selected-reference"),
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
    toast: $("#toast"),
  };

  const state = {
    sessions: [],
    current: null,
    turns: [],
    surfaces: [],
    selectedSurfaceId: null,
    attachments: [],
    busy: false,
    stageAtLiveEdge: true,
    newSurfaceCount: 0,
    config: null,
    artifactResizeTimer: null,
    avatarTimer: null,
    markup: {
      surface: null,
      image: null,
      strokes: [],
      liveStroke: null,
      tool: "pen",
      color: "#ff4d4f",
      width: 6,
      ready: false,
    },
  };

  const THINKING_STATES = ["working", "composing", "solving"];

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
        /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
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
    state.current = null;
    state.turns = [];
    state.surfaces = [];
    state.selectedSurfaceId = null;
    els.sessionTitle.textContent = "Choose or start a session";
    els.archiveSession.disabled = true;
    els.input.disabled = true;
    els.send.disabled = true;
    renderChat();
    renderStage();
  }

  async function selectSession(id) {
    if (state.busy) return;
    const data = await api(`/api/calliope/sessions/${encodeURIComponent(id)}`);
    state.current = data.session;
    state.turns = data.turns || [];
    state.surfaces = data.surfaces || [];
    state.selectedSurfaceId = null;
    state.newSurfaceCount = 0;
    els.sessionTitle.textContent = state.current.title;
    els.archiveSession.disabled = false;
    els.input.disabled = false;
    els.send.disabled = false;
    renderSessions();
    renderSelected();
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
    return ({ query: "▤", metric: "◆", artifact: "▦", image: "▧", document: "▱" })[kind] || "◇";
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
    const value = surface.payload?.result;
    const display = Array.isArray(value) || (value && typeof value === "object")
      ? JSON.stringify(value)
      : formatValue(value);
    return `<div class="metric-body">
      <div class="metric-value">${escapeHtml(display)}</div>
      <div class="metric-caption">${escapeHtml(surface.title)}${
        surface.payload?.data_as_of ? ` · as of ${escapeHtml(surface.payload.data_as_of)}` : ""
      }</div>
    </div>`;
  }

  function renderArtifact(surface) {
    const url = surface.payload?.display_url || surface.payload?.url;
    if (!url) return `<div class="chart-empty">Artifact URL unavailable</div>`;
    return `<div class="artifact-frame">
      <iframe src="${escapeHtml(url)}" title="${escapeHtml(surface.title)}"
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
        : `<div class="chart-empty">Capture saved at ${escapeHtml(surface.payload?.path || "the warehouse")}</div>`
    }</div>`;
  }

  function renderDocument(surface) {
    const path = surface.payload?.path;
    return `<div class="document-body"><div class="document-glyph">§</div>
      <div>${escapeHtml(surface.payload?.bytes ? `${Number(surface.payload.bytes).toLocaleString()} bytes` : "Rendered document")}</div>
      ${path ? `<a href="${escapeHtml(path)}" target="_blank" rel="noopener">Open document</a>` : ""}
    </div>`;
  }

  function surfaceCard(surface) {
    const meta = [
      surface.artifact_version ? `v${surface.artifact_version}` : null,
      relativeTime(surface.created_at),
    ].filter(Boolean).join(" · ");
    const metadata = surface.kind === "query" && isMetadataQuery(surface);
    const body = ({
      query: renderQuery,
      metric: renderMetric,
      artifact: renderArtifact,
      image: renderImage,
      document: renderDocument,
    })[surface.kind]?.(surface) || `<div class="chart-empty">Surface unavailable</div>`;
    const openUrl = surface.kind === "artifact"
      ? surface.payload?.display_url || surface.payload?.url
      : surface.kind === "document" ? surface.payload?.path : null;
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
    }" data-surface-id="${escapeHtml(surface.id)}">
      <header class="surface-head">
        <span class="surface-kind">${surfaceGlyph(surface.kind)} ${metadata ? "metadata" : escapeHtml(surface.kind)}</span>
        <div class="surface-titles"><h3>${escapeHtml(surface.title)}</h3><p>${escapeHtml(meta)}</p></div>
        <div class="surface-tools">
          ${surface.kind === "image" && surface.payload?.overlay_image_url
            ? `<button type="button" data-toggle-markup="${escapeHtml(surface.id)}" aria-pressed="true" title="Hide or show markup">Marks</button>`
            : ""}
          ${surface.kind === "image" && surface.payload?.image_url
            ? `<button type="button" data-markup-surface="${escapeHtml(surface.id)}" title="Draw on this image">Markup</button>`
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
    if (initial) {
      els.stageScroll.scrollTop = 0;
      state.stageAtLiveEdge = true;
    }
  }

  function focusSurface(id) {
    const surface = state.surfaces.find((item) => item.id === id);
    if (!surface) return;
    state.selectedSurfaceId = id;
    setMobilePanel();
    renderSelected();
    $$(".surface.selected").forEach((element) => element.classList.remove("selected"));
    const element = $(`.surface[data-surface-id="${CSS.escape(id)}"]`);
    if (element) {
      element.classList.add("selected");
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      element.animate?.([
        { boxShadow: "0 0 0 1px rgba(245,180,70,.9),0 0 45px rgba(245,180,70,.28)" },
        { boxShadow: "0 0 0 1px rgba(245,180,70,.18),0 18px 40px rgba(0,0,0,.35)" },
      ], { duration: 900 });
    }
  }

  function renderSelected() {
    const surface = state.surfaces.find((item) => item.id === state.selectedSurfaceId);
    if (!surface) {
      els.selectedReference.hidden = true;
      els.selectedReference.innerHTML = "";
      return;
    }
    els.selectedReference.hidden = false;
    els.selectedReference.innerHTML = `${surfaceGlyph(surface.kind)} Referencing <strong>${escapeHtml(surface.title)}</strong>
      <button type="button" data-clear-selection aria-label="Clear reference">×</button>`;
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
    els.markupUndo.disabled = !markup.strokes.length;
    els.markupClear.disabled = !markup.strokes.length;
  }

  function resetMarkup() {
    state.markup.surface = null;
    state.markup.image = null;
    state.markup.strokes = [];
    state.markup.liveStroke = null;
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

  function openMarkup(surfaceId) {
    const surface = state.surfaces.find((item) => item.id === surfaceId);
    const url = surface?.payload?.image_url;
    if (!surface || !url) {
      toast("That image is not available for markup", true);
      return;
    }
    resetMarkup();
    state.markup.surface = surface;
    els.markupTitle.textContent = `Mark up · ${surface.title}`;
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
      },
    });
    state.selectedSurfaceId = source.id;
    renderSelected();
    $$(".surface.selected").forEach((element) => element.classList.remove("selected"));
    $(`.surface[data-surface-id="${CSS.escape(source.id)}"]`)?.classList.add("selected");
    renderAttachmentTray();
    closeMarkup();
    setMobilePanel("chat");
    els.input.focus();
    toast("Annotated image added to the next message");
  }

  async function readFiles(files) {
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
      state.attachments.push({ name: file.name, data_url: dataUrl });
    }
    renderAttachmentTray();
  }

  function renderAttachmentTray() {
    els.attachmentTray.hidden = !state.attachments.length;
    els.attachmentTray.innerHTML = state.attachments.map((attachment, index) => `
      <div class="attachment-preview">
        <img src="${escapeHtml(attachment.data_url)}" alt="${escapeHtml(attachment.name)}">
        ${attachment.annotation ? `<span class="annotation-badge">markup</span>` : ""}
        <button type="button" data-remove-attachment="${index}" aria-label="Remove ${escapeHtml(attachment.name)}">×</button>
      </div>`).join("");
  }

  function resizeComposer() {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(180, els.input.scrollHeight)}px`;
  }

  function optimisticTurn(message) {
    const maxOrdinal = Math.max(0, ...state.turns.map((turn) => Number(turn.ordinal || 0)));
    const turn = {
      id: `pending-${Date.now()}`,
      ordinal: maxOrdinal + 1,
      user_message: message || "[Image]",
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
    if (!message && !state.attachments.length) return;
    const outgoingAttachments = [...state.attachments];
    const pending = optimisticTurn(message);
    els.input.value = "";
    state.attachments = [];
    renderAttachmentTray();
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
          selected_surface_id: state.selectedSurfaceId,
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
    const known = ["run_sql_multi", "run_sql", "create_live_app", "update_live_app", "publish_dashboard", "update_dashboard", "capture_live_app", "render_pdf", "metric"];
    const found = known.find((tool) => String(name || "").endsWith(tool));
    return (found || raw.slice(-2).join("_")).replaceAll("_", " ");
  }

  function setupEvents() {
    els.mobileSessions.addEventListener("click", () => {
      setMobilePanel(document.body.classList.contains("mobile-sessions-open") ? null : "sessions");
    });
    els.mobileChat.addEventListener("click", () => {
      setMobilePanel(document.body.classList.contains("mobile-chat-open") ? null : "chat");
    });
    els.mobileShade.addEventListener("click", () => setMobilePanel());
    window.addEventListener("resize", () => {
      if (!window.matchMedia("(max-width: 880px)").matches) setMobilePanel();
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
      state.selectedSurfaceId = null;
      renderSelected();
      $$(".surface.selected").forEach((element) => element.classList.remove("selected"));
    });
    els.messages.addEventListener("click", (event) => {
      const button = event.target.closest("[data-focus-surface]");
      if (button) focusSurface(button.dataset.focusSurface);
    });
    els.stage.addEventListener("click", (event) => {
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
      const source = event.target.closest("[data-source-turn]");
      if (source) {
        jumpToTurn(source.dataset.sourceTurn);
        return;
      }
      const card = event.target.closest("[data-surface-id]");
      if (card && !event.target.closest("a,button,summary")) focusSurface(card.dataset.surfaceId);
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
    setupEvents();
    try {
      await loadConfig();
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
