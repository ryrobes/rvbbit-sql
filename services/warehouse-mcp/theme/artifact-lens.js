(() => {
  "use strict";

  const dashboard = window.RVBBIT_DASHBOARD || {};
  if (!dashboard.slug || dashboard.historical || window.self !== window.top) return;

  const AS_OF_PARAM = "rvbbit_as_of";
  const SCRUB_DEBOUNCE_MS = 1100;
  const RESULT_BATCH_SIZE = 100;
  const openKey = `rvbbit-artifact-lens-open:${dashboard.slug}`;
  const scrollKey = `rvbbit-artifact-lens-scroll:${dashboard.slug}`;
  const positionKey = `rvbbit-artifact-lens-position:${dashboard.slug}`;
  const SENSITIVE = /(?:secret|token|password|passwd|auth|cookie|session|api[-_]?key)/i;
  const MEANINGFUL = [
    "a", "button", "input", "select", "textarea", "[role]", "[aria-label]", "[title]",
    "canvas", "svg", "table", "th", "td", "h1", "h2", "h3", "h4", "h5", "h6",
    "[data-field]", "[data-series]", "[data-metric]", "[data-dimension]", "[data-testid]",
  ].join(",");
  let activeAsOf = new URL(window.location.href).searchParams.get(AS_OF_PARAM);
  let timeline = null;
  let loading = null;
  let applyTimer = null;
  let pickerActive = false;
  let inspectionBusy = false;
  let pickedElement = null;
  let currentInspection = null;
  let hoverFrame = null;
  let candidateTimer = null;
  let viewExplicitlyChosen = false;

  function sessionGet(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function sessionSet(key, value) {
    try {
      if (value === null) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, value);
    } catch {
      // Storage is only a convenience; the URL remains the source of truth.
    }
  }

  function localGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function localSet(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch {
      // A movable Lens still works when browser persistence is unavailable.
    }
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function boundedText(value, limit = 400) {
    return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function formatPoint(value, includeTime = true) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "Unknown time";
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year: parsed.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
      hour: includeTime ? "numeric" : undefined,
      minute: includeTime ? "2-digit" : undefined,
    }).format(parsed);
  }

  function formatValue(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "number") {
      return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
    }
    if (typeof value === "object") return boundedText(JSON.stringify(value), 240);
    return boundedText(value, 240);
  }

  function formatDelta(delta) {
    if (!delta || !Number.isFinite(Number(delta.absolute))) return "";
    const absolute = Number(delta.absolute);
    const percent = Number(delta.percent);
    const sign = absolute > 0 ? "+" : "";
    return `${sign}${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(absolute)}${
      Number.isFinite(percent) ? ` · ${percent > 0 ? "+" : ""}${percent.toFixed(1)}%` : ""
    }`;
  }

  function closestPoint(points, target) {
    if (!target || !points.length) return points.length - 1;
    const wanted = Date.parse(target);
    if (!Number.isFinite(wanted)) return points.length - 1;
    let best = 0;
    let distance = Number.POSITIVE_INFINITY;
    points.forEach((point, index) => {
      const next = Math.abs(Date.parse(point) - wanted);
      if (next < distance) {
        best = index;
        distance = next;
      }
    });
    return best;
  }

  function restoreScroll() {
    const raw = sessionGet(scrollKey);
    sessionSet(scrollKey, null);
    const y = Number(raw);
    if (!Number.isFinite(y) || y <= 0) return;
    window.addEventListener("load", () => {
      window.setTimeout(() => window.scrollTo({ top: y, behavior: "auto" }), 80);
    }, { once: true });
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { accept: "application/json", ...(options.headers || {}) },
      ...options,
    });
    let data;
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    if (!response.ok || data.error) {
      throw new Error(data.error?.message || `Request failed (${response.status})`);
    }
    return data;
  }

  function fetchTimeline() {
    const version = dashboard.version ? `?version=${encodeURIComponent(dashboard.version)}` : "";
    return fetchJson(`/api/d/${encodeURIComponent(dashboard.slug)}/time-travel${version}`);
  }

  function safeCss(value) {
    if (window.CSS?.escape) return window.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, (char) => `\\${char}`);
  }

  function selectorFor(element) {
    if (!(element instanceof Element)) return "";
    const parts = [];
    let node = element;
    while (node && node !== document.body && parts.length < 7) {
      let part = node.localName || "div";
      if (node.id && !SENSITIVE.test(node.id)) {
        part += `#${safeCss(node.id)}`;
        parts.unshift(part);
        break;
      }
      const testId = node.getAttribute("data-testid");
      if (testId && !SENSITIVE.test(testId)) {
        part += `[data-testid="${safeCss(testId)}"]`;
      } else if (node.parentElement) {
        const siblings = [...node.parentElement.children].filter(
          (sibling) => sibling.localName === node.localName,
        );
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ").slice(0, 800);
  }

  function safeData(element) {
    const data = {};
    [...element.attributes].slice(0, 40).forEach((attribute) => {
      const key = attribute.name;
      if (
        !SENSITIVE.test(key)
        && (key.startsWith("data-") || ["aria-label", "title", "name"].includes(key))
      ) {
        data[key] = boundedText(attribute.value, 240);
      }
    });
    return data;
  }

  function tableContext(element) {
    const cell = element.closest("td,th");
    if (!cell) return null;
    const row = cell.parentElement;
    const table = cell.closest("table");
    if (!row || !table) return null;
    const rows = [...table.querySelectorAll("tbody tr")];
    const cells = [...row.children].filter((child) => child.matches("td,th"));
    const columnIndex = cells.indexOf(cell);
    const headers = [...table.querySelectorAll("thead tr:last-child th")];
    return {
      row_index: Math.max(0, rows.indexOf(row)),
      column_index: Math.max(0, columnIndex),
      column_header: boundedText(headers[columnIndex]?.textContent, 160),
      cell_text: boundedText(cell.textContent, 400),
    };
  }

  function chartContext(element, event) {
    const canvas = element.closest("canvas");
    const ChartApi = window.Chart;
    if (!canvas || !ChartApi?.getChart) return null;
    const chart = ChartApi.getChart(canvas);
    if (!chart) return null;
    let hit;
    try {
      hit = chart.getElementsAtEventForMode(
        event,
        "nearest",
        { intersect: true },
        true,
      )[0];
    } catch {
      return null;
    }
    if (!hit) return null;
    const datasetIndex = Number(hit.datasetIndex);
    const dataIndex = Number(hit.index);
    const dataset = chart.data?.datasets?.[datasetIndex] || {};
    const raw = dataset.data?.[dataIndex];
    let parsed = null;
    try {
      parsed = chart.getDatasetMeta(datasetIndex)?.controller?.getParsed(dataIndex);
    } catch {
      // Parsed coordinates are useful but not required for a binding.
    }
    return {
      dataset_index: datasetIndex,
      data_index: dataIndex,
      dataset_label: boundedText(dataset.label, 240),
      data_label: boundedText(chart.data?.labels?.[dataIndex], 240),
      value: formatValue(
        typeof raw === "object" && raw !== null
          ? (raw.y ?? raw.x ?? raw.r ?? JSON.stringify(raw))
          : raw,
      ),
      raw,
      parsed,
    };
  }

  function svgContext(element, event) {
    const svg = element?.matches?.("svg") ? element : element?.closest?.("svg");
    if (!svg) return null;
    const path = event?.composedPath?.() || [];
    const mark = path.find((node) => (
      node instanceof Element
      && node !== svg
      && svg.contains(node)
      && node.namespaceURI === "http://www.w3.org/2000/svg"
    )) || null;
    const indexedMarks = [...svg.querySelectorAll("[data-i],[data-index],[data-row-index]")];
    const indexedMark = mark?.closest?.("[data-i],[data-index],[data-row-index]");
    const rawIndex = indexedMark?.getAttribute("data-row-index")
      ?? indexedMark?.getAttribute("data-index")
      ?? indexedMark?.getAttribute("data-i");
    const parsedIndex = Number(rawIndex);
    const markText = boundedText(
      mark?.querySelector?.("title")?.textContent
        || (mark?.matches?.("text,title") ? mark.textContent : "")
        || indexedMark?.getAttribute("aria-label")
        || indexedMark?.getAttribute("title"),
      240,
    );
    const container = svg.closest("section,article,.card,[role='figure']") || svg.parentElement;
    const containerLabel = boundedText(
      container?.querySelector?.("h1,h2,h3,h4,h5,h6,[aria-label]")?.textContent
        || container?.getAttribute?.("aria-label"),
      240,
    );
    const textValues = [...svg.querySelectorAll("title,text")]
      .map((node) => boundedText(node.textContent, 120))
      .filter(Boolean)
      .slice(0, 160);
    return {
      row_index: Number.isInteger(parsedIndex) && parsedIndex >= 0 ? parsedIndex : null,
      indexed_mark_count: indexedMarks.length,
      mark_tag: mark?.localName || "",
      mark_text: markText,
      container_label: containerLabel,
      text_values: textValues,
      data: safeData(indexedMark || mark || svg),
    };
  }

  function meaningfulElement(event) {
    const path = event.composedPath?.() || [];
    if (path.some((node) => node?.localName === "rvbbit-artifact-lens")) return null;
    const source = path.find((node) => node instanceof Element && node.localName !== "rvbbit-artifact-lens");
    if (!(source instanceof Element) || source === document.body || source === document.documentElement) {
      return null;
    }
    if (source.closest("rvbbit-artifact-lens")) return null;
    const semantic = source.closest(MEANINGFUL);
    if (semantic && semantic !== document.body && semantic !== document.documentElement) return semantic;
    let node = source;
    while (node && node !== document.body) {
      const rect = node.getBoundingClientRect();
      const text = boundedText(node.textContent, 900);
      if (rect.width >= 8 && rect.height >= 8 && text && text.length <= 900) return node;
      node = node.parentElement;
    }
    return source;
  }

  function describeElement(element, event) {
    const rect = element.getBoundingClientRect();
    const table = tableContext(element);
    const chart = chartContext(element, event);
    const visual = svgContext(element, event);
    const text = boundedText(element.textContent, 600);
    const aria = boundedText(
      element.getAttribute("aria-label")
        || element.getAttribute("title")
        || element.getAttribute("data-metric")
        || element.getAttribute("data-field"),
      400,
    );
    let label = aria
      || table?.cell_text
      || chart?.dataset_label
      || visual?.mark_text
      || visual?.container_label
      || text
      || element.localName;
    if (chart?.data_label) label = `${chart.dataset_label || "Chart"} · ${chart.data_label}`;
    return {
      label: boundedText(label, 400),
      selector: selectorFor(element),
      tag: element.localName,
      role: boundedText(element.getAttribute("role"), 80),
      text,
      data: safeData(element),
      bounds: {
        x: Math.max(0, rect.left),
        y: Math.max(0, rect.top),
        width: Math.max(1, rect.width),
        height: Math.max(1, rect.height),
      },
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        scroll_x: window.scrollX,
        scroll_y: window.scrollY,
        document_width: document.documentElement.scrollWidth,
        document_height: document.documentElement.scrollHeight,
      },
      click: { x: event.clientX, y: event.clientY },
      ...(table ? { table } : {}),
      ...(chart ? {
        chart: {
          dataset_index: chart.dataset_index,
          data_index: chart.data_index,
          dataset_label: chart.dataset_label,
          data_label: chart.data_label,
          value: chart.value,
        },
      } : {}),
      ...(visual ? {
        visual: {
          row_index: visual.row_index,
          indexed_mark_count: visual.indexed_mark_count,
          mark_tag: visual.mark_tag,
          mark_text: visual.mark_text,
          container_label: visual.container_label,
          text_values: visual.text_values,
          data: visual.data,
        },
      } : {}),
      _chart: chart,
      _visual: visual,
    };
  }

  function columnNames(trace) {
    return (trace?.columns || []).map((column) => (
      typeof column === "string" ? column : column?.name
    )).filter(Boolean);
  }

  function rowObject(row, columns) {
    if (row && typeof row === "object" && !Array.isArray(row)) return row;
    if (Array.isArray(row)) {
      return Object.fromEntries(columns.map((column, index) => [column, row[index]]));
    }
    return {};
  }

  function normalized(value) {
    return String(value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function comparable(value) {
    return String(value ?? "")
      .trim()
      .toLowerCase()
      .replace(/[$,%\s]/g, "")
      .replace(/[()]/g, "");
  }

  function isValueLikeText(value) {
    const text = boundedText(value, 120);
    if (!text || text.length > 48) return false;
    return /^[~≈<>≤≥]?\s*[+-]?\s*(?:[$€£¥]\s*)?\(?\d[\d,]*(?:\.\d+)?\)?(?:\s*%)?(?:\s*[kmbt])?$/i.test(text);
  }

  function evidenceKey(value) {
    const text = boundedText(value, 120);
    if (!text || text.length > 80) return "";
    let key = normalized(text);
    if (key.endsWith("county")) key = key.slice(0, -6);
    if (!key || (/^\d+$/.test(key) && key.length < 4)) return "";
    return key;
  }

  function visualTextKeys(values) {
    const keys = new Set();
    (values || []).forEach((value) => {
      [value, ...String(value).split(/[:·|,/–—]+/)].forEach((part) => {
        const key = evidenceKey(part);
        if (key) keys.add(key);
      });
    });
    return keys;
  }

  function valuesMatch(left, right) {
    const a = comparable(left);
    const b = comparable(right);
    if (!a || !b) return false;
    if (a === b) return true;
    const an = Number(a);
    const bn = Number(b);
    return Number.isFinite(an) && Number.isFinite(bn)
      && Math.abs(an - bn) <= Math.max(0.000001, Math.abs(an) * 0.00001);
  }

  function fieldMatch(columns, hints) {
    const wanted = hints.map(normalized).filter(Boolean);
    return columns.find((column) => wanted.includes(normalized(column))) || "";
  }

  function traceEntries() {
    try {
      const entries = dashboard.queryTrace?.();
      return Array.isArray(entries) ? entries.filter((entry) => !entry.error) : [];
    } catch {
      return [];
    }
  }

  function visualBindingEvidence(trace, visual) {
    if (!visual) return { score: 0, exact: false };
    const columns = columnNames(trace);
    const rows = trace.rows || [];
    const indexedCount = Number(visual.indexed_mark_count);
    const rowIndex = Number(visual.row_index);
    const traceRowCount = Number(trace.row_count);
    const indexedCoverage = traceRowCount > 0 ? indexedCount / traceRowCount : 0;
    if (
      Number.isInteger(rowIndex)
      && rowIndex >= 0
      && indexedCount > 0
      && rowIndex < traceRowCount
      && indexedCoverage >= 0.25
    ) {
      const matchedRow = rowIndex < rows.length ? rowObject(rows[rowIndex], columns) : null;
      const identityField = columns.find((column) => (
        /(?:^id$|_id$|id$)/i.test(column)
        && matchedRow?.[column] !== undefined
        && matchedRow?.[column] !== null
      )) || "";
      return {
        score: indexedCoverage >= 0.8 ? 14 : 8,
        exact: indexedCoverage >= 0.8,
        field: identityField,
        row: matchedRow,
        rowIndex,
        matchedValue: (
          (identityField && matchedRow?.[identityField])
          || visual.mark_text
          || `mark ${rowIndex + 1}`
        ),
      };
    }

    const keys = visualTextKeys(visual.text_values);
    if (!keys.size) return { score: 0, exact: false };
    const fieldHits = new Map();
    rows.forEach((rawRow) => {
      const row = rowObject(rawRow, columns);
      Object.entries(row).forEach(([field, value]) => {
        const key = evidenceKey(value);
        if (!key || !keys.has(key)) return;
        if (!fieldHits.has(field)) fieldHits.set(field, new Set());
        fieldHits.get(field).add(key);
      });
    });
    const ranked = [...fieldHits.entries()]
      .map(([field, hits]) => ({ field, count: hits.size }))
      .sort((left, right) => right.count - left.count);
    const best = ranked[0];
    if (!best) return { score: 0, exact: false };
    return {
      score: best.count >= 2 ? 11 : 5,
      exact: best.count >= 2,
      field: best.field,
      row: null,
      rowIndex: null,
      matchedValue: best.count === 1 ? "1 visible value" : `${best.count} visible values`,
    };
  }

  function resolveBinding(target) {
    const traces = traceEntries();
    const table = target.table;
    const chart = target._chart;
    const visual = target._visual;
    const dataHints = Object.entries(target.data || {})
      .filter(([key]) => /(?:field|metric|series|column|dimension)/i.test(key))
      .map(([, value]) => value);
    const fieldHints = [
      table?.column_header,
      chart?.dataset_label,
      ...dataHints,
    ].filter(Boolean);
    const labelHints = [
      chart?.data_label,
      target.data?.["data-label"],
      target.data?.["data-dimension"],
    ].filter(Boolean);
    const valueHints = [
      table?.cell_text,
      chart?.value,
      target.data?.["data-value"],
    ].filter((value) => value !== undefined && value !== null && value !== "");
    // A number inside prose is not evidence that the prose was produced by the
    // query. Keep heuristic matching to value-shaped DOM nodes; tables, charts,
    // and explicit data-* contracts have stronger context above.
    const visibleValues = isValueLikeText(target.text) ? [target.text] : [];
    let best = null;
    const candidates = [];

    traces.forEach((trace, traceIndex) => {
      const columns = columnNames(trace);
      const hintedField = fieldMatch(columns, fieldHints);
      const rowCountMatch = visibleValues.some((hint) => valuesMatch(trace.row_count, hint));
      const visualEvidence = visualBindingEvidence(trace, visual);
      let traceBest = {
        score: visualEvidence.score || (hintedField ? 4 : rowCountMatch ? 5 : 0),
        exact: visualEvidence.exact,
        field: visualEvidence.field || hintedField || (rowCountMatch ? "row_count" : ""),
        row: visualEvidence.row || null,
        rowIndex: visualEvidence.rowIndex ?? null,
        matchedValue: visualEvidence.matchedValue || (rowCountMatch ? trace.row_count : valueHints[0] || ""),
        rowCountMatch,
      };
      (trace.rows || []).forEach((rawRow, rowIndex) => {
        const row = rowObject(rawRow, columns);
        let score = hintedField ? 4 : 0;
        let field = hintedField;
        let matchedValue = "";
        if (hintedField && valueHints.some((value) => valuesMatch(row[hintedField], value))) {
          score += 8;
          matchedValue = row[hintedField];
        }
        const labelMatches = labelHints.filter((label) => (
          Object.values(row).some((value) => valuesMatch(value, label))
        )).length;
        score += labelMatches * 3;
        if (!field) {
          for (const [key, value] of Object.entries(row)) {
            if (valueHints.some((hint) => valuesMatch(value, hint))) {
              field = key;
              matchedValue = value;
              score += chart ? 6 : 5;
              break;
            }
          }
        }
        if (chart && field && labelMatches) score += 3;
        if (!field && !table && !chart) {
          for (const [key, value] of Object.entries(row)) {
            if (visibleValues.some((hint) => valuesMatch(value, hint))) {
              field = key;
              matchedValue = value;
              score += 3;
              break;
            }
          }
        }
        if (score > traceBest.score) {
          traceBest = { score, field, row, rowIndex, matchedValue };
        }
      });
      const candidate = { ...traceBest, trace, traceIndex };
      candidates.push(candidate);
      if (!best || candidate.score > best.score) best = candidate;
    });

    // Traces are candidates, not ambient provenance. In particular, a
    // one-query dashboard must not make every literal label inherit that query.
    if (!best || best.score < 3) {
      return {
        kind: table ? "table" : chart || visual ? "chart" : target.text ? "value" : "element",
        confidence: "visual",
        field: table?.column_header || chart?.dataset_label || "",
        label: target.label,
        value: table?.cell_text || chart?.value || "",
      };
    }
    const exact = (
      best.exact
      || (best.rowCountMatch && candidates.filter((candidate) => candidate.rowCountMatch).length === 1)
      || (table && best.score >= 10)
      || (chart && best.score >= 10)
      || (dataHints.length && best.score >= 8)
    );
    const confidence = exact ? "exact" : best.score >= 3 ? "likely" : "visual";
    return {
      kind: table ? "table" : chart || visual ? "chart" : target.text ? "value" : "element",
      confidence,
      field: best.field || table?.column_header || chart?.dataset_label || "",
      label: (
        visual
        && best.row
        && (target.label === visual.container_label || target.label === visual.mark_tag)
        && (best.row.title || best.row.name)
      ) || target.label,
      value: formatValue(best.matchedValue || table?.cell_text || chart?.value || ""),
      trace_row_index: best.rowIndex,
      row_index: table?.row_index,
      column_index: table?.column_index,
      dataset_index: chart?.dataset_index,
      data_index: chart?.data_index,
      row: best.row,
      _trace: best.trace,
    };
  }

  function tracePayload(binding) {
    const trace = binding._trace;
    if (!trace) return {};
    return {
      id: trace.id,
      query_hash: trace.query_hash,
      sql: trace.sql,
      as_of: trace.as_of,
      columns: trace.columns,
      row_count: trace.row_count,
      truncated: trace.truncated,
      engine: trace.engine,
      elapsed_ms: trace.elapsed_ms,
    };
  }

  function publicTarget(target) {
    const { _chart, _visual, ...clean } = target;
    return clean;
  }

  function publicBinding(binding) {
    const { _trace, ...clean } = binding;
    return clean;
  }

  function mount() {
    if (document.querySelector("rvbbit-artifact-lens")) return;

    const host = document.createElement("rvbbit-artifact-lens");
    host.style.cssText = [
      "all:initial",
      "position:fixed",
      "z-index:2147483000",
      "right:max(18px,env(safe-area-inset-right))",
      "bottom:max(18px,env(safe-area-inset-bottom))",
      "width:auto",
      "height:auto",
      "pointer-events:none",
      "color-scheme:dark",
    ].join(";");
    const root = host.attachShadow({ mode: "open" });
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/theme/artifact-lens.css";
    root.appendChild(stylesheet);

    const shell = document.createElement("div");
    shell.className = "lens";
    shell.dataset.open = "false";
    shell.dataset.active = activeAsOf ? "true" : "false";
    shell.dataset.view = "time";
    shell.innerHTML = `
      <div class="candidate-layer" aria-hidden="true"></div>
      <div class="target-outline" aria-hidden="true"></div>
      <div class="picker-hint" hidden>Pick a value, chart point, table cell, or visual object · Esc to cancel</div>
      <section class="panel" aria-label="Artifact lens" aria-hidden="true" tabindex="-1">
        <header title="Drag to move · double-click to reset">
          <span class="eyebrow">Artifact lens</span>
          <span class="drag-grip" aria-hidden="true">⠿</span>
          <button class="close" type="button" aria-label="Minimize artifact lens">×</button>
          <strong>Ask the artifact why.</strong>
          <small>Explore retained data time or trace a rendered object back to its evidence.</small>
        </header>
        <nav class="tabs" aria-label="Artifact lens modes">
          <button type="button" class="tab active" data-view="time">Data time</button>
          <button type="button" class="tab" data-view="trace">Trace</button>
        </nav>
        <div class="view time-view">
          <div class="loading timeline-loading" role="status">
            <i aria-hidden="true"></i>
            <span>Reading retained history…</span>
          </div>
          <div class="unavailable timeline-unavailable" hidden>
            <b>Time travel unavailable</b>
            <span class="unavailable-copy"></span>
          </div>
          <div class="controls" hidden>
            <div class="selection">
              <span>Viewing</span>
              <strong class="selection-value">Latest data</strong>
              <small class="selection-note">The authored dashboard is unchanged.</small>
            </div>
            <input class="scrubber" type="range" min="0" max="1" step="1" value="1"
              aria-label="Dashboard data time">
            <div class="range-labels">
              <span class="range-start">Earlier</span>
              <span>Latest</span>
            </div>
            <div class="actions">
              <span class="apply-state" aria-live="polite"></span>
              <button class="latest" type="button">Return to latest</button>
            </div>
            <p class="coverage"></p>
          </div>
        </div>
        <div class="view trace-view" hidden>
          <div class="trace-intro">
            <span class="trace-glyph" aria-hidden="true">⌖</span>
            <strong>Select something you want to understand.</strong>
            <p>Lens binds tables, SVG and Chart.js marks, and contracted values to query evidence when it can. Other objects retain artifact-level source context.</p>
            <button class="pick" type="button">Pick from dashboard</button>
          </div>
          <div class="trace-loading loading" hidden role="status">
            <i aria-hidden="true"></i>
            <span>Following the evidence…</span>
          </div>
          <div class="trace-result" hidden></div>
        </div>
        <footer>RVBBIT evidence sidecar · dashboard code stays unchanged</footer>
      </section>
      <aside class="query-drawer" data-side="left" aria-label="Query result" aria-hidden="true">
        <header class="query-drawer-head">
          <div>
            <span class="eyebrow">Live result set</span>
            <strong class="query-drawer-title">Traced query</strong>
            <small class="query-drawer-meta">Authenticated · governed read-only execution</small>
          </div>
          <div class="query-drawer-actions">
            <button class="query-refresh" type="button" aria-label="Run query again" title="Run query again">↻</button>
            <button class="query-drawer-close" type="button" aria-label="Close query result">×</button>
          </div>
        </header>
        <div class="query-drawer-content">
          <div class="query-drawer-loading" hidden role="status">
            <i aria-hidden="true"></i>
            <span>Running the traced query…</span>
          </div>
          <div class="query-drawer-error" hidden></div>
          <div class="query-table-wrap" hidden>
            <table class="query-table"></table>
          </div>
          <div class="query-empty" hidden>No rows returned.</div>
        </div>
        <footer class="query-drawer-foot">
          <span class="query-row-status">Ready to run</span>
          <div class="query-drawer-foot-actions">
            <button class="query-more" type="button" hidden>Show 100 more</button>
            <button class="query-analyze" type="button" hidden>Analyze with Calliope</button>
          </div>
        </footer>
      </aside>
      <button class="trigger" type="button" aria-expanded="false" aria-label="Open artifact lens">
        <span class="clock" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M12 7v5l3 2M5.6 5.7A8.5 8.5 0 1 1 3.5 12"/>
            <path d="M3.5 4.5v5h5"/>
          </svg>
        </span>
        <span class="trigger-copy">
          <b>Artifact lens</b>
          <small>${activeAsOf ? formatPoint(activeAsOf, false) : "Live · trace"}</small>
        </span>
        <i aria-hidden="true"></i>
      </button>`;
    root.appendChild(shell);
    document.body.appendChild(host);

    const panel = root.querySelector(".panel");
    const panelHeader = panel.querySelector("header");
    const trigger = root.querySelector(".trigger");
    const close = root.querySelector(".close");
    const tabs = [...root.querySelectorAll(".tab")];
    const timeView = root.querySelector(".time-view");
    const traceView = root.querySelector(".trace-view");
    const loadingNode = root.querySelector(".timeline-loading");
    const unavailable = root.querySelector(".timeline-unavailable");
    const unavailableCopy = root.querySelector(".unavailable-copy");
    const controls = root.querySelector(".controls");
    const scrubber = root.querySelector(".scrubber");
    const selectionValue = root.querySelector(".selection-value");
    const selectionNote = root.querySelector(".selection-note");
    const rangeStart = root.querySelector(".range-start");
    const applyState = root.querySelector(".apply-state");
    const latest = root.querySelector(".latest");
    const coverage = root.querySelector(".coverage");
    const triggerStatus = root.querySelector(".trigger-copy small");
    const pick = root.querySelector(".pick");
    const traceIntro = root.querySelector(".trace-intro");
    const traceLoading = root.querySelector(".trace-loading");
    const traceResult = root.querySelector(".trace-result");
    const candidateLayer = root.querySelector(".candidate-layer");
    const outline = root.querySelector(".target-outline");
    const pickerHint = root.querySelector(".picker-hint");
    const queryDrawer = root.querySelector(".query-drawer");
    const queryDrawerTitle = root.querySelector(".query-drawer-title");
    const queryDrawerMeta = root.querySelector(".query-drawer-meta");
    const queryDrawerLoading = root.querySelector(".query-drawer-loading");
    const queryDrawerError = root.querySelector(".query-drawer-error");
    const queryTableWrap = root.querySelector(".query-table-wrap");
    const queryTable = root.querySelector(".query-table");
    const queryEmpty = root.querySelector(".query-empty");
    const queryRowStatus = root.querySelector(".query-row-status");
    const queryMore = root.querySelector(".query-more");
    const queryAnalyze = root.querySelector(".query-analyze");
    const queryRefresh = root.querySelector(".query-refresh");
    const queryDrawerClose = root.querySelector(".query-drawer-close");
    let queryResult = null;
    let queryVisibleRows = RESULT_BATCH_SIZE;
    let queryRunning = false;
    let dragState = null;

    function setHostPosition(left, top) {
      host.style.left = `${Math.round(left)}px`;
      host.style.top = `${Math.round(top)}px`;
      host.style.right = "auto";
      host.style.bottom = "auto";
    }

    function lensBounds() {
      const rects = [trigger.getBoundingClientRect()];
      if (shell.dataset.open === "true") rects.push(panel.getBoundingClientRect());
      return {
        left: Math.min(...rects.map((rect) => rect.left)),
        top: Math.min(...rects.map((rect) => rect.top)),
        right: Math.max(...rects.map((rect) => rect.right)),
        bottom: Math.max(...rects.map((rect) => rect.bottom)),
      };
    }

    function positionQueryDrawer() {
      if (shell.dataset.drawerOpen !== "true") return;
      const margin = window.innerWidth <= 540 ? 8 : 12;
      const gap = 10;
      const panelRect = panel.getBoundingClientRect();
      const availableLeft = Math.max(0, panelRect.left - gap - margin);
      const availableRight = Math.max(0, window.innerWidth - margin - panelRect.right - gap);
      let side = availableLeft >= availableRight ? "left" : "right";
      let available = side === "left" ? availableLeft : availableRight;
      const overlay = window.innerWidth < 760 || available < 300;
      const width = overlay
        ? Math.max(280, window.innerWidth - margin * 2)
        : Math.min(720, available);
      const height = Math.min(680, window.innerHeight - margin * 2);
      const top = Math.max(
        margin,
        Math.min(panelRect.top, window.innerHeight - margin - height),
      );
      let left = margin;
      if (!overlay) {
        left = side === "left"
          ? panelRect.left - gap - width
          : panelRect.right + gap;
      } else {
        side = "overlay";
      }
      queryDrawer.dataset.side = side;
      queryDrawer.style.left = `${Math.round(left)}px`;
      queryDrawer.style.top = `${Math.round(top)}px`;
      queryDrawer.style.width = `${Math.round(width)}px`;
      queryDrawer.style.height = `${Math.round(height)}px`;
    }

    function constrainLens() {
      const left = Number.parseFloat(host.style.left);
      const top = Number.parseFloat(host.style.top);
      if (!Number.isFinite(left) || !Number.isFinite(top)) {
        positionQueryDrawer();
        return;
      }
      const margin = window.innerWidth <= 540 ? 8 : 12;
      const bounds = lensBounds();
      let dx = 0;
      let dy = 0;
      if (bounds.left < margin) dx = margin - bounds.left;
      else if (bounds.right > window.innerWidth - margin) {
        dx = window.innerWidth - margin - bounds.right;
      }
      if (bounds.top < margin) dy = margin - bounds.top;
      else if (bounds.bottom > window.innerHeight - margin) {
        dy = window.innerHeight - margin - bounds.bottom;
      }
      if (dx || dy) setHostPosition(left + dx, top + dy);
      positionQueryDrawer();
    }

    function persistLensPosition() {
      const left = Number.parseFloat(host.style.left);
      const top = Number.parseFloat(host.style.top);
      if (!Number.isFinite(left) || !Number.isFinite(top)) return;
      localSet(positionKey, JSON.stringify({ left, top }));
    }

    function restoreLensPosition() {
      const raw = localGet(positionKey);
      if (!raw) return;
      try {
        const saved = JSON.parse(raw);
        const left = Number(saved.left);
        const top = Number(saved.top);
        if (Number.isFinite(left) && Number.isFinite(top)) {
          setHostPosition(left, top);
          constrainLens();
        }
      } catch {
        localSet(positionKey, null);
      }
    }

    function resetLensPosition() {
      localSet(positionKey, null);
      host.style.left = "auto";
      host.style.top = "auto";
      host.style.right = "max(18px,env(safe-area-inset-right))";
      host.style.bottom = "max(18px,env(safe-area-inset-bottom))";
      window.requestAnimationFrame(() => {
        positionQueryDrawer();
        scheduleCandidateHighlights();
      });
    }

    function beginLensDrag(event) {
      if (
        event.button !== 0
        || event.target.closest("button,a,input,select,textarea")
      ) return;
      const triggerRect = trigger.getBoundingClientRect();
      if (!Number.isFinite(Number.parseFloat(host.style.left))) {
        setHostPosition(triggerRect.left, triggerRect.top);
      }
      dragState = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startLeft: Number.parseFloat(host.style.left),
        startTop: Number.parseFloat(host.style.top),
        bounds: lensBounds(),
      };
      panelHeader.setPointerCapture?.(event.pointerId);
      shell.dataset.dragging = "true";
      event.preventDefault();
    }

    function moveLens(event) {
      if (!dragState || event.pointerId !== dragState.pointerId) return;
      const margin = window.innerWidth <= 540 ? 8 : 12;
      let dx = event.clientX - dragState.startX;
      let dy = event.clientY - dragState.startY;
      dx = Math.max(
        margin - dragState.bounds.left,
        Math.min(dx, window.innerWidth - margin - dragState.bounds.right),
      );
      dy = Math.max(
        margin - dragState.bounds.top,
        Math.min(dy, window.innerHeight - margin - dragState.bounds.bottom),
      );
      setHostPosition(dragState.startLeft + dx, dragState.startTop + dy);
      positionQueryDrawer();
      scheduleCandidateHighlights();
      event.preventDefault();
    }

    function endLensDrag(event) {
      if (!dragState || event.pointerId !== dragState.pointerId) return;
      panelHeader.releasePointerCapture?.(event.pointerId);
      dragState = null;
      shell.dataset.dragging = "false";
      persistLensPosition();
      constrainLens();
    }

    function traceValueIndex() {
      const values = new Set();
      const evidenceValues = new Set();
      const rowCounts = new Set();
      const columns = new Set();
      traceEntries().forEach((trace) => {
        rowCounts.add(comparable(trace.row_count));
        columnNames(trace).forEach((column) => columns.add(normalized(column)));
        (trace.rows || []).slice(0, 200).forEach((rawRow) => {
          const row = rowObject(rawRow, columnNames(trace));
          Object.values(row).forEach((value) => {
            if (
              value !== null
              && value !== undefined
              && (typeof value === "number" || String(value).length <= 120)
            ) {
              const key = comparable(value);
              if (key) values.add(key);
              const evidence = evidenceKey(value);
              if (evidence) evidenceValues.add(evidence);
            }
          });
        });
      });
      return { values, evidenceValues, rowCounts, columns };
    }

    function candidateConfidence(element, index) {
      if (!index.values.size && !index.rowCounts.size) return null;
      if (element.matches("td,th")) return "exact";
      if (element.matches("canvas") && window.Chart?.getChart?.(element)) return "exact";
      if (element.matches("svg")) {
        const indexedCount = element.querySelectorAll("[data-i],[data-index],[data-row-index]").length;
        const indexedCoverage = traceEntries()
          .map((trace) => indexedCount / Math.max(1, Number(trace.row_count)))
          .filter((ratio) => ratio > 0);
        if (indexedCoverage.some((ratio) => ratio >= 0.8 && ratio <= 1)) return "exact";
        const keys = visualTextKeys(
          [...element.querySelectorAll("title,text")].map((node) => node.textContent),
        );
        const matches = [...keys].filter((key) => index.evidenceValues.has(key)).length;
        if (matches >= 2) return "exact";
        if (indexedCoverage.some((ratio) => ratio >= 0.25 && ratio <= 1)) return "likely";
        if (matches === 1) return "likely";
        return null;
      }
      const hints = [
        element.getAttribute("data-field"),
        element.getAttribute("data-metric"),
        element.getAttribute("data-series"),
        element.getAttribute("data-dimension"),
      ].filter(Boolean);
      if (hints.some((hint) => index.columns.has(normalized(hint)))) return "exact";
      const text = boundedText(element.textContent, 120);
      if (!isValueLikeText(text)) return null;
      const numbers = text.match(/[-+]?[$]?\d[\d,.]*(?:%?)/g) || [];
      if (numbers.some((value) => index.rowCounts.has(comparable(value)))) return "likely";
      if (numbers.some((value) => index.values.has(comparable(value)))) return "likely";
      return null;
    }

    function boxesOverlap(left, right) {
      return (
        left.left < right.right
        && left.right > right.left
        && left.top < right.bottom
        && left.bottom > right.top
      );
    }

    function renderCandidateHighlights() {
      candidateLayer.replaceChildren();
      if (shell.dataset.open !== "true" || shell.dataset.view !== "trace") return;
      const index = traceValueIndex();
      if (!index.values.size && !index.rowCounts.size) return;
      const candidates = new Set(document.querySelectorAll([
        "td", "th", "canvas", "svg",
        "[data-field]", "[data-metric]", "[data-series]", "[data-dimension]",
      ].join(",")));
      [...document.body.querySelectorAll("*")].slice(0, 3500).forEach((element) => {
        if (
          element.localName === "rvbbit-artifact-lens"
          || element.childElementCount > 1
          || !/\d/.test(element.textContent || "")
        ) return;
        const text = boundedText(element.textContent, 120);
        if (text && text.length <= 120) candidates.add(element);
      });
      const panelRect = panel.getBoundingClientRect();
      const triggerRect = trigger.getBoundingClientRect();
      const drawerRect = shell.dataset.drawerOpen === "true"
        ? queryDrawer.getBoundingClientRect()
        : null;
      const items = [];
      candidates.forEach((element) => {
        const confidence = candidateConfidence(element, index);
        if (!confidence) return;
        const rect = element.getBoundingClientRect();
        if (
          rect.width < 4
          || rect.height < 4
          || rect.right <= 0
          || rect.bottom <= 0
          || rect.left >= window.innerWidth
          || rect.top >= window.innerHeight
          || boxesOverlap(rect, panelRect)
          || boxesOverlap(rect, triggerRect)
          || (drawerRect && boxesOverlap(rect, drawerRect))
        ) return;
        items.push({ element, rect, confidence });
      });
      const leaves = items.filter((item) => !items.some((other) => (
        other !== item
        && item.element.contains(other.element)
        && boundedText(item.element.textContent, 120) === boundedText(other.element.textContent, 120)
      )));
      leaves
        .sort((left, right) => (
          (left.confidence === right.confidence ? 0 : left.confidence === "exact" ? -1 : 1)
          || (left.rect.width * left.rect.height) - (right.rect.width * right.rect.height)
        ))
        .slice(0, 96)
        .forEach(({ rect, confidence }) => {
          const box = document.createElement("i");
          box.className = `candidate-box ${confidence}`;
          box.style.left = `${Math.max(0, rect.left)}px`;
          box.style.top = `${Math.max(0, rect.top)}px`;
          box.style.width = `${Math.max(1, rect.width)}px`;
          box.style.height = `${Math.max(1, rect.height)}px`;
          candidateLayer.appendChild(box);
        });
    }

    function scheduleCandidateHighlights() {
      window.clearTimeout(candidateTimer);
      if (shell.dataset.open !== "true" || shell.dataset.view !== "trace") {
        candidateLayer.replaceChildren();
        return;
      }
      candidateTimer = window.setTimeout(renderCandidateHighlights, 80);
    }

    function setView(view, explicit = false) {
      const next = view === "trace" ? "trace" : "time";
      if (explicit) viewExplicitlyChosen = true;
      shell.dataset.view = next;
      tabs.forEach((tab) => {
        const active = tab.dataset.view === next;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      timeView.hidden = next !== "time";
      traceView.hidden = next !== "trace";
      if (next === "time") {
        closeQueryDrawer();
        if (pickerActive) stopPicker();
        candidateLayer.replaceChildren();
        loadTimeline();
      } else {
        scheduleCandidateHighlights();
        if (currentInspection && !pickerActive) startPicker();
      }
    }

    function setOpen(open) {
      const openedFromTrigger = open && root.activeElement === trigger;
      shell.dataset.open = String(open);
      panel.setAttribute("aria-hidden", String(!open));
      trigger.setAttribute("aria-expanded", String(open));
      trigger.setAttribute("aria-hidden", String(open));
      trigger.setAttribute("aria-label", open ? "Minimize artifact lens" : "Open artifact lens");
      trigger.tabIndex = open ? -1 : 0;
      sessionSet(openKey, open ? "1" : null);
      if (open && shell.dataset.view === "time") loadTimeline();
      if (open && shell.dataset.view === "trace") {
        scheduleCandidateHighlights();
        if (currentInspection && !pickerActive) startPicker();
      }
      if (!open && pickerActive) stopPicker();
      if (!open) {
        candidateLayer.replaceChildren();
        closeQueryDrawer();
      }
      if (openedFromTrigger) panel.focus({ preventScroll: true });
      window.requestAnimationFrame(constrainLens);
    }

    function setActive(value) {
      activeAsOf = value || null;
      dashboard.as_of = activeAsOf;
      shell.dataset.active = activeAsOf ? "true" : "false";
      triggerStatus.textContent = activeAsOf ? formatPoint(activeAsOf, false) : "Live · trace";
    }

    function renderTimeline(data) {
      loadingNode.hidden = true;
      const firstResolution = timeline === null;
      timeline = data;
      if (!data.eligible || !Array.isArray(data.points) || data.points.length < 2) {
        unavailable.hidden = false;
        unavailableCopy.textContent = data.message || "No common retained history was found.";
        controls.hidden = true;
        if (firstResolution && !viewExplicitlyChosen && !activeAsOf) setView("trace");
        return;
      }
      unavailable.hidden = true;
      controls.hidden = false;
      scrubber.min = "0";
      scrubber.max = String(data.points.length - 1);
      const index = closestPoint(data.points, activeAsOf);
      scrubber.value = String(index);
      rangeStart.textContent = formatPoint(data.earliest, false);
      coverage.textContent = `${data.table_count} source${data.table_count === 1 ? "" : "s"} · `
        + `${data.point_count} retained data points`;
      renderSelection(data.points[index], Boolean(activeAsOf));
    }

    function renderSelection(point, historical) {
      if (historical) {
        selectionValue.textContent = formatPoint(point);
        selectionNote.textContent = "AS-OF snapshot · dashboard queries are pinned to this point";
      } else {
        selectionValue.textContent = "Latest data";
        selectionNote.textContent = timeline?.latest_refresh
          ? `Most recent retained refresh ${formatPoint(timeline.latest_refresh)}`
          : "The authored dashboard is unchanged.";
      }
    }

    async function loadTimeline() {
      if (timeline || loading) return loading;
      loadingNode.hidden = false;
      unavailable.hidden = true;
      controls.hidden = true;
      loading = (async () => {
        try {
          let data = await fetchTimeline();
          if (data.code === "NO_QUERY_SOURCES") {
            await new Promise((resolve) => window.setTimeout(resolve, 700));
            data = await fetchTimeline();
          }
          renderTimeline(data);
        } catch (error) {
          renderTimeline({
            eligible: false,
            message: error instanceof Error ? error.message : "The timeline could not be loaded.",
          });
        } finally {
          loading = null;
        }
      })();
      return loading;
    }

    async function applyAsOf(point) {
      window.clearTimeout(applyTimer);
      applyTimer = null;
      const next = new URL(window.location.href);
      if (point) next.searchParams.set(AS_OF_PARAM, point);
      else next.searchParams.delete(AS_OF_PARAM);
      if (next.href === window.location.href) {
        applyState.textContent = "";
        return;
      }

      applyState.textContent = "Refreshing all queries…";
      sessionSet(openKey, "1");
      sessionSet(scrollKey, String(window.scrollY));
      setActive(point);
      const refresh = window.RVBBIT_DASHBOARD?.refresh;
      if (typeof refresh === "function") {
        window.history.replaceState({}, "", next);
        try {
          await refresh({ as_of: point || null });
          applyState.textContent = "Updated";
          renderSelection(point || timeline.points[timeline.points.length - 1], Boolean(point));
          return;
        } catch {
          // A dashboard-provided refresh is optional; a full reload is the safe fallback.
        }
      }
      window.location.assign(next.href);
    }

    function scheduleAsOf(point) {
      window.clearTimeout(applyTimer);
      applyState.textContent = "Waiting for you to stop scrubbing…";
      applyTimer = window.setTimeout(() => applyAsOf(point), SCRUB_DEBOUNCE_MS);
    }

    function positionOutline(element, selected = false) {
      if (!element?.isConnected) {
        outline.hidden = true;
        return;
      }
      const rect = element.getBoundingClientRect();
      outline.hidden = false;
      outline.dataset.selected = String(selected);
      outline.style.left = `${Math.max(0, rect.left)}px`;
      outline.style.top = `${Math.max(0, rect.top)}px`;
      outline.style.width = `${Math.max(1, rect.width)}px`;
      outline.style.height = `${Math.max(1, rect.height)}px`;
    }

    function onPickerMove(event) {
      if (!pickerActive) return;
      window.cancelAnimationFrame(hoverFrame);
      hoverFrame = window.requestAnimationFrame(() => {
        const element = meaningfulElement(event);
        if (element) positionOutline(element);
      });
    }

    async function inspectElement(element, event) {
      if (inspectionBusy) return;
      inspectionBusy = true;
      closeQueryDrawer();
      queryResult = null;
      pickedElement = element;
      positionOutline(element, true);
      const target = describeElement(element, event);
      const binding = resolveBinding(target);
      const cleanTarget = publicTarget(target);
      const cleanBinding = publicBinding(binding);
      setOpen(true);
      setView("trace");
      traceIntro.hidden = true;
      traceResult.hidden = true;
      traceLoading.hidden = false;
      pickerHint.textContent = "Click another highlighted object to replace this trace · Esc to close";
      try {
        currentInspection = await fetchJson(
          `/api/d/${encodeURIComponent(dashboard.slug)}/inspect`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              version: dashboard.version,
              target: cleanTarget,
              binding: cleanBinding,
              trace: tracePayload(binding),
            }),
          },
        );
        renderInspection(currentInspection);
      } catch (error) {
        currentInspection = null;
        traceResult.innerHTML = `
          <div class="trace-error">
            <b>Evidence unavailable</b>
            <span>${escapeHtml(error instanceof Error ? error.message : error)}</span>
            <small>Click another highlighted object to try a different trace.</small>
          </div>`;
        traceResult.hidden = false;
      } finally {
        inspectionBusy = false;
        traceLoading.hidden = true;
        scheduleCandidateHighlights();
      }
    }

    function onPickerClick(event) {
      if (!pickerActive) return;
      const element = meaningfulElement(event);
      if (!element) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (inspectionBusy || queryRunning) return;
      inspectElement(element, event);
    }

    function onPickerKey(event) {
      if (event.key !== "Escape" || !pickerActive) return;
      event.preventDefault();
      if (shell.dataset.drawerOpen === "true") {
        closeQueryDrawer();
        return;
      }
      setOpen(false);
      trigger.focus();
    }

    function startPicker() {
      if (pickerActive) return;
      pickerActive = true;
      shell.dataset.picking = "true";
      pickerHint.hidden = false;
      pickerHint.textContent = currentInspection
        ? "Click another highlighted object to replace this trace · Esc to close"
        : "Pick a highlighted value, chart point, table cell, or visual object · Esc to close";
      pick.textContent = "Picking…";
      pick.setAttribute("aria-pressed", "true");
      document.documentElement.classList.add("rvbbit-artifact-lens-picking");
      document.addEventListener("pointermove", onPickerMove, true);
      document.addEventListener("click", onPickerClick, true);
      document.addEventListener("keydown", onPickerKey, true);
    }

    function stopPicker(keepOutline = false) {
      pickerActive = false;
      shell.dataset.picking = "false";
      pickerHint.hidden = true;
      pick.textContent = "Pick from dashboard";
      pick.setAttribute("aria-pressed", "false");
      document.documentElement.classList.remove("rvbbit-artifact-lens-picking");
      document.removeEventListener("pointermove", onPickerMove, true);
      document.removeEventListener("click", onPickerClick, true);
      document.removeEventListener("keydown", onPickerKey, true);
      window.cancelAnimationFrame(hoverFrame);
      if (!keepOutline) outline.hidden = true;
    }

    function openQueryDrawer() {
      shell.dataset.drawerOpen = "true";
      queryDrawer.setAttribute("aria-hidden", "false");
      positionQueryDrawer();
      window.requestAnimationFrame(positionQueryDrawer);
      scheduleCandidateHighlights();
    }

    function closeQueryDrawer() {
      shell.dataset.drawerOpen = "false";
      queryDrawer.setAttribute("aria-hidden", "true");
      scheduleCandidateHighlights();
    }

    function resultColumns(result) {
      const declared = Array.isArray(result?.columns) ? result.columns : [];
      const columns = declared.map((column) => ({
        name: typeof column === "string" ? column : column?.name,
        type: typeof column === "object" && column ? column.type : "",
      })).filter((column) => column.name);
      if (columns.length) return columns;
      const first = (result?.rows || []).find((row) => (
        row && typeof row === "object" && !Array.isArray(row)
      ));
      return first ? Object.keys(first).map((name) => ({ name, type: "" })) : [];
    }

    function queryCell(value) {
      if (value === null || value === undefined) {
        return '<span class="query-null">NULL</span>';
      }
      let text;
      if (typeof value === "object") {
        try {
          text = JSON.stringify(value);
        } catch {
          text = String(value);
        }
      } else {
        text = String(value);
      }
      const clipped = text.length > 8000 ? `${text.slice(0, 8000)}…` : text;
      const expandable = clipped.length > 52 || /[\r\n]/.test(clipped);
      return `<span class="query-cell${expandable ? " expandable" : ""}"${
        expandable ? ' tabindex="0" role="button" aria-label="Expand cell"' : ""
      }>${escapeHtml(clipped)}</span>`;
    }

    function renderQueryRows() {
      if (!queryResult) return;
      const rows = Array.isArray(queryResult.rows) ? queryResult.rows : [];
      const columns = resultColumns(queryResult);
      const names = columns.map((column) => column.name);
      const visible = rows.slice(0, queryVisibleRows);
      const scrollTop = queryTableWrap.scrollTop;
      const scrollLeft = queryTableWrap.scrollLeft;
      queryTable.innerHTML = `
        <thead><tr>${columns.map((column) => (
          `<th><span>${escapeHtml(column.name)}</span>${
            column.type ? `<small>${escapeHtml(column.type)}</small>` : ""
          }</th>`
        )).join("")}</tr></thead>
        <tbody>${visible.map((rawRow, rowIndex) => {
          const row = rowObject(rawRow, names);
          return `<tr data-row="${rowIndex}">${names.map((name) => (
            `<td>${queryCell(row[name])}</td>`
          )).join("")}</tr>`;
        }).join("")}</tbody>`;
      queryTableWrap.hidden = !rows.length;
      queryEmpty.hidden = Boolean(rows.length);
      const returned = rows.length;
      const shown = visible.length;
      const capped = queryResult.truncated ? "+" : "";
      queryRowStatus.textContent = rows.length
        ? `Showing ${shown} of ${returned}${capped} returned rows`
        : "Query completed · 0 rows";
      queryMore.hidden = shown >= returned;
      queryMore.textContent = `Show ${Math.min(RESULT_BATCH_SIZE, returned - shown)} more`;
      queryTableWrap.scrollTop = scrollTop;
      queryTableWrap.scrollLeft = scrollLeft;
      positionQueryDrawer();
    }

    function renderQueryResult(result) {
      queryResult = result;
      queryVisibleRows = RESULT_BATCH_SIZE;
      const columns = resultColumns(result);
      const rows = Array.isArray(result.rows) ? result.rows : [];
      const provenance = currentInspection?.provenance || {};
      const rowLabel = `${rows.length}${result.truncated ? "+" : ""} row${rows.length === 1 ? "" : "s"}`;
      queryDrawerTitle.textContent = currentInspection?.selection?.label
        || provenance.query_hash
        || "Traced query";
      queryDrawerMeta.textContent = [
        result.engine || provenance.engine || "read-only",
        rowLabel,
        `${columns.length} column${columns.length === 1 ? "" : "s"}`,
        Number.isFinite(Number(result.elapsed_ms)) ? `${Number(result.elapsed_ms)} ms` : "",
        result.as_of_applied ? `AS-OF ${formatPoint(result.as_of_applied)}` : "latest data",
      ].filter(Boolean).join(" · ");
      queryDrawerLoading.hidden = true;
      queryDrawerError.hidden = true;
      queryAnalyze.hidden = !currentInspection?.calliope_enabled;
      queryAnalyze.disabled = false;
      queryAnalyze.textContent = "Analyze with Calliope";
      renderQueryRows();
    }

    async function runInspectionQuery(button = null) {
      const provenance = currentInspection?.provenance || {};
      const sql = String(provenance.sql || "").trim();
      if (!sql || queryRunning) return;
      queryRunning = true;
      root.querySelectorAll(".query-run").forEach((node) => {
        node.disabled = true;
        node.textContent = "Running…";
      });
      queryRefresh.disabled = true;
      queryDrawerTitle.textContent = currentInspection?.selection?.label || "Traced query";
      queryDrawerMeta.textContent = provenance.as_of
        ? `Preparing retained snapshot · ${formatPoint(provenance.as_of)}`
        : "Preparing latest governed result";
      queryDrawerLoading.hidden = false;
      queryDrawerError.hidden = true;
      queryTableWrap.hidden = true;
      queryEmpty.hidden = true;
      queryMore.hidden = true;
      queryAnalyze.hidden = true;
      queryRowStatus.textContent = "Executing read-only SQL…";
      openQueryDrawer();
      try {
        const result = await fetchJson(
          `/api/d/${encodeURIComponent(dashboard.slug)}/q`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              sql,
              as_of: provenance.as_of || activeAsOf || null,
            }),
          },
        );
        renderQueryResult(result);
      } catch (error) {
        queryResult = null;
        queryDrawerLoading.hidden = true;
        queryTableWrap.hidden = true;
        queryEmpty.hidden = true;
        queryDrawerError.hidden = false;
        queryDrawerError.innerHTML = `
          <b>Query unavailable</b>
          <span>${escapeHtml(error instanceof Error ? error.message : error)}</span>`;
        queryRowStatus.textContent = "Execution failed";
      } finally {
        queryRunning = false;
        queryRefresh.disabled = false;
        root.querySelectorAll(".query-run").forEach((node) => {
          node.disabled = false;
          node.textContent = "Run query";
        });
        if (button?.isConnected) button.focus();
        positionQueryDrawer();
      }
    }

    function sourceCard(source) {
      const freshness = source.freshness || {};
      let status = "Catalog source";
      if (freshness.last_synced) {
        status = `Synced ${formatPoint(freshness.last_synced)}${freshness.stale ? " · drift detected" : ""}`;
      }
      return `
        <div class="evidence-card source-card">
          <span>Source</span>
          <strong>${escapeHtml(source.table)}</strong>
          <small>${escapeHtml(status)}</small>
          ${source.doc ? `<p>${escapeHtml(source.doc)}</p>` : ""}
        </div>`;
    }

    function renderInspection(data) {
      const provenance = data.provenance || {};
      const binding = data.binding || {};
      const selection = data.selection || {};
      const comparison = data.comparison;
      const confidence = provenance.confidence || binding.confidence || "visual";
      const selectedLabel = (
        confidence !== "visual"
        && binding.label
        && binding.label !== selection.label
      ) ? binding.label : (selection.label || binding.label || "Selected target");
      const selectedContext = (
        selection.label
        && selection.label !== selectedLabel
      ) ? `${selection.label} · ` : "";
      const queryCard = provenance.sql
        ? `<details class="evidence-card query-card">
            <summary>
              <span>Query · ${escapeHtml(confidence)}</span>
              <strong>${escapeHtml(provenance.query_hash || "traced SQL")}</strong>
              <small>${escapeHtml(provenance.engine || "read-only")} · ${(provenance.tables || []).length} source${(provenance.tables || []).length === 1 ? "" : "s"}</small>
            </summary>
            <pre>${escapeHtml(provenance.sql)}</pre>
          </details>`
        : `<div class="evidence-card query-card weak">
            <span>Visual binding</span>
            <strong>No direct query binding was found</strong>
            <small>Showing artifact-level sources without inventing a data binding.</small>
          </div>`;
      const comparisonCard = comparison
        ? `<div class="evidence-card comparison-card">
            <span>Then → now${comparison.field ? ` · ${escapeHtml(comparison.field)}` : ""}</span>
            <div class="compare-values">
              <strong>${escapeHtml(formatValue(comparison.current))}</strong>
              <i>→</i>
              <strong>${escapeHtml(formatValue(comparison.latest))}</strong>
            </div>
            <small>${escapeHtml(formatPoint(comparison.as_of))}${comparison.delta ? ` · ${escapeHtml(formatDelta(comparison.delta))}` : ""}</small>
          </div>`
        : "";
      const sources = (data.sources || []).length
        ? data.sources.map(sourceCard).join("")
        : `<div class="evidence-card weak"><span>Sources</span><strong>No source edge discovered yet</strong><small>The selection is still preserved as rendered evidence.</small></div>`;
      const related = (data.related_artifacts || []).length
        ? `<div class="related">
            <span>Also uses this evidence</span>
            <div>${data.related_artifacts.map((artifact) => (
              `<a href="/d/${encodeURIComponent(artifact.slug)}" target="_blank" rel="noopener">${escapeHtml(artifact.name)}</a>`
            )).join("")}</div>
          </div>`
        : "";
      traceResult.innerHTML = `
        <div class="trace-heading">
          <span class="confidence ${escapeHtml(confidence)}">${escapeHtml(confidence)} binding</span>
          <strong>${escapeHtml(selectedLabel)}</strong>
          <small>${escapeHtml(selectedContext)}${escapeHtml(binding.field || selection.tag || "rendered object")}${
            binding.value ? ` · ${escapeHtml(binding.value)}` : ""
          }</small>
        </div>
        <div class="evidence-stack">
          ${comparisonCard}
          ${queryCard}
          ${sources}
        </div>
        ${related}
        <div class="trace-actions">
          <span class="trace-reselect">Click another highlighted object to replace this trace.</span>
          ${provenance.sql ? '<button type="button" class="query-run">Run query</button>' : ""}
          ${data.calliope_enabled ? '<button type="button" class="calliope-investigate">Ask Calliope</button>' : ""}
        </div>`;
      traceResult.hidden = false;
      window.requestAnimationFrame(constrainLens);
      scheduleCandidateHighlights();
    }

    function queryResultPreview() {
      if (!queryResult) return null;
      const columns = resultColumns(queryResult).slice(0, 40);
      const names = columns.map((column) => column.name);
      const rows = (queryResult.rows || []).slice(0, 12).map((rawRow) => {
        const row = rowObject(rawRow, names);
        return Object.fromEntries(names.map((name) => {
          const value = row[name];
          if (value === null || value === undefined || typeof value !== "object") {
            return [name, typeof value === "string" ? value.slice(0, 800) : value];
          }
          try {
            return [name, JSON.stringify(value).slice(0, 800)];
          } catch {
            return [name, String(value).slice(0, 800)];
          }
        }));
      });
      return {
        query_hash: currentInspection?.provenance?.query_hash || null,
        columns,
        rows,
        preview_rows: rows.length,
        row_count: queryResult.row_count,
        returned_rows: Array.isArray(queryResult.rows) ? queryResult.rows.length : 0,
        truncated: Boolean(queryResult.truncated),
        engine: queryResult.engine || null,
        elapsed_ms: queryResult.elapsed_ms,
        as_of_applied: queryResult.as_of_applied || null,
      };
    }

    async function launchCalliope(button, analyzeResult = false) {
      if (!currentInspection) return;
      const preview = analyzeResult ? queryResultPreview() : null;
      if (analyzeResult && !preview) return;
      const idleLabel = analyzeResult ? "Analyze with Calliope" : "Ask Calliope";
      button.disabled = true;
      button.textContent = "Opening new session…";
      const pending = window.open("about:blank", "_blank");
      try {
        const inspection = analyzeResult
          ? { ...currentInspection, query_result: preview }
          : currentInspection;
        const data = await fetchJson("/api/calliope/investigations", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            slug: dashboard.slug,
            version: currentInspection.artifact?.version || dashboard.version,
            target: currentInspection.selection,
            inspection,
            mode: analyzeResult ? "query_result" : "selection",
          }),
        });
        if (!data.new_session) throw new Error("Calliope did not create a new investigation session");
        if (analyzeResult && data.mode !== "query_result") {
          throw new Error("Calliope did not preserve the result-set context");
        }
        if (pending) pending.location.href = data.url;
        else window.location.href = data.url;
        button.textContent = "Opened";
      } catch (error) {
        pending?.close();
        button.disabled = false;
        button.textContent = idleLabel;
        const note = document.createElement("span");
        note.className = "handoff-error";
        note.textContent = error instanceof Error ? error.message : String(error);
        button.parentElement?.appendChild(note);
      }
    }

    function askCalliope(button) {
      return launchCalliope(button, false);
    }

    function analyzeQueryWithCalliope(button) {
      return launchCalliope(button, true);
    }

    trigger.addEventListener("click", () => setOpen(shell.dataset.open !== "true"));
    close.addEventListener("click", () => {
      setOpen(false);
      trigger.focus();
    });
    tabs.forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view, true)));
    pick.addEventListener("click", startPicker);
    scrubber.addEventListener("input", () => {
      if (!timeline) return;
      const point = timeline.points[Number(scrubber.value)];
      renderSelection(point, true);
      scheduleAsOf(point);
    });
    latest.addEventListener("click", () => {
      if (timeline) {
        scrubber.value = String(timeline.points.length - 1);
        renderSelection(timeline.points[timeline.points.length - 1], false);
      }
      applyAsOf(null);
    });
    traceResult.addEventListener("click", (event) => {
      const query = event.target.closest(".query-run");
      if (query) {
        runInspectionQuery(query);
        return;
      }
      const handoff = event.target.closest(".calliope-investigate");
      if (handoff) askCalliope(handoff);
    });
    queryDrawer.addEventListener("click", (event) => {
      const analyze = event.target.closest(".query-analyze");
      if (analyze) {
        analyzeQueryWithCalliope(analyze);
        return;
      }
      if (event.target.closest(".query-drawer-close")) {
        closeQueryDrawer();
        return;
      }
      if (event.target.closest(".query-refresh")) {
        runInspectionQuery();
        return;
      }
      if (event.target.closest(".query-more")) {
        queryVisibleRows += RESULT_BATCH_SIZE;
        renderQueryRows();
        return;
      }
      const cell = event.target.closest(".query-cell.expandable");
      if (cell) cell.classList.toggle("expanded");
    });
    queryDrawer.addEventListener("keydown", (event) => {
      const cell = event.target.closest(".query-cell.expandable");
      if (!cell || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      cell.classList.toggle("expanded");
    });
    panelHeader.addEventListener("pointerdown", beginLensDrag);
    panelHeader.addEventListener("pointermove", moveLens);
    panelHeader.addEventListener("pointerup", endLensDrag);
    panelHeader.addEventListener("pointercancel", endLensDrag);
    panelHeader.addEventListener("dblclick", (event) => {
      if (event.target.closest("button,a,input,select,textarea")) return;
      event.preventDefault();
      resetLensPosition();
    });
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && shell.dataset.open === "true" && !pickerActive) {
        event.preventDefault();
        if (shell.dataset.drawerOpen === "true") closeQueryDrawer();
        else {
          setOpen(false);
          trigger.focus();
        }
      }
    });
    window.addEventListener("scroll", () => {
      if (pickedElement) positionOutline(pickedElement, true);
      scheduleCandidateHighlights();
    }, { passive: true });
    window.addEventListener("resize", () => {
      if (pickedElement) positionOutline(pickedElement, true);
      constrainLens();
      scheduleCandidateHighlights();
    });
    window.addEventListener("rvbbit:query-trace", () => {
      if (shell.dataset.view === "trace") {
        const count = traceEntries().length;
        pick.title = count ? `${count} dashboard quer${count === 1 ? "y" : "ies"} traced` : "";
        scheduleCandidateHighlights();
      }
    });
    const candidateObserver = new MutationObserver(() => scheduleCandidateHighlights());
    candidateObserver.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    const initializePosition = () => {
      restoreLensPosition();
      constrainLens();
    };
    stylesheet.addEventListener("load", initializePosition, { once: true });
    window.requestAnimationFrame(() => window.requestAnimationFrame(initializePosition));
    if (sessionGet(openKey) === "1") setOpen(true);
    restoreScroll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
})();
