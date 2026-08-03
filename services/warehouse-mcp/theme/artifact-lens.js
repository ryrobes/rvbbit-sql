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
  const SQL_KEYWORDS = new Set(`
    all alter analyze and any array as asc asof at between both by case cast check
    collate column constraint create cross current_date current_time current_timestamp
    database default delete desc distinct do else end except exists false fetch filter
    first following for foreign from full function generated group grouping having if
    ilike in index inner insert intersect interval into is join lateral leading left
    like limit materialized natural not null nulls offset on only or order outer over
    partition preceding primary qualify range recursive references returning right row
    rows schema select set table tablesample then ties to trailing true truncate union
    unique unbounded update using values view when where window with within
    bigint bigserial bit boolean bytea char date decimal double enum float int integer
    json jsonb numeric real serial smallint text time timestamp uuid varchar
  `.trim().split(/\s+/));
  const SQL_TOKEN = /(?:--[^\r\n]*|\/\*[\s\S]*?\*\/|'(?:''|\\[\s\S]|[^'\\])*'|"(?:""|\\[\s\S]|[^"\\])*"|`(?:``|\\[\s\S]|[^`\\])*`|\{\{[\s\S]*?\}\}|\$\d+|:[a-zA-Z_][a-zA-Z0-9_]*|\b(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\b|\b[a-zA-Z_][a-zA-Z0-9_$]*\b|#>>|->>|::|#>|->|<>|!=|<=|>=|=>|:=|\|\||&&|[-+*/%=<>&|^~])/gi;
  const SQL_FORMAT_TOKEN = new RegExp(`${SQL_TOKEN.source}|[(),.;]|\\s+|.`, "gis");
  const SQL_FORMAT_PHRASES = [
    [["left", "outer", "join"], "join"],
    [["right", "outer", "join"], "join"],
    [["full", "outer", "join"], "join"],
    [["left", "join"], "join"],
    [["right", "join"], "join"],
    [["full", "join"], "join"],
    [["inner", "join"], "join"],
    [["cross", "join"], "join"],
    [["natural", "join"], "join"],
    [["group", "by"], "group"],
    [["order", "by"], "order"],
    [["partition", "by"], "partition"],
    [["union", "all"], "setop"],
  ];
  const SQL_MAJOR_CLAUSES = new Map([
    ["with", "with"], ["select", "select"], ["from", "from"],
    ["where", "where"], ["having", "having"], ["qualify", "qualify"],
    ["window", "window"], ["limit", "limit"], ["offset", "offset"],
    ["fetch", "fetch"], ["returning", "returning"], ["values", "values"],
    ["set", "set"], ["union", "setop"], ["intersect", "setop"],
    ["except", "setop"], ["join", "join"],
  ]);
  const MEANINGFUL = [
    "a", "button", "input", "select", "textarea", "[role]", "[aria-label]", "[title]",
    "canvas", "svg", "table", "th", "td", "h1", "h2", "h3", "h4", "h5", "h6",
    "[data-rvbbit-object]", "[data-field]", "[data-series]", "[data-metric]",
    "[data-dimension]", "[data-testid]",
  ].join(",");
  let activeAsOf = new URL(window.location.href).searchParams.get(AS_OF_PARAM);
  let timeline = null;
  let loading = null;
  let applyTimer = null;
  let pickerActive = false;
  let inspectionBusy = false;
  let pickedElement = null;
  let currentInspection = null;
  let currentTrail = null;
  let trailHistory = [];
  let trailRequest = 0;
  let watchRequest = 0;
  let currentWatches = null;
  let watchPanelOpen = false;
  let watchError = "";
  let hoverFrame = null;
  let candidateTimer = null;
  let semanticPollTimer = null;
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

  function highlightSql(value) {
    const sql = String(value ?? "");
    let cursor = 0;
    let highlighted = "";
    SQL_TOKEN.lastIndex = 0;
    for (const match of sql.matchAll(SQL_TOKEN)) {
      const token = match[0];
      const index = match.index ?? cursor;
      highlighted += escapeHtml(sql.slice(cursor, index));
      const lower = token.toLowerCase();
      let kind = "";
      if (token.startsWith("--") || token.startsWith("/*")) kind = "comment";
      else if (token.startsWith("'")) kind = "string";
      else if (token.startsWith('"') || token.startsWith("`")) kind = "identifier";
      else if (token.startsWith("{{") || /^\$\d+$/.test(token) || /^:[a-z_]/i.test(token)) kind = "parameter";
      else if (/^(?:\d|\.\d)/.test(token)) kind = "number";
      else if (SQL_KEYWORDS.has(lower)) kind = "keyword";
      else if (/^[a-z_]/i.test(token) && /^\s*\(/.test(sql.slice(index + token.length))) kind = "function";
      else if (/^[#:\-+*/%=<>&|^~]/.test(token)) kind = "operator";
      highlighted += kind
        ? `<span class="sql-${kind}">${escapeHtml(token)}</span>`
        : escapeHtml(token);
      cursor = index + token.length;
    }
    return highlighted + escapeHtml(sql.slice(cursor));
  }

  function formatSql(value) {
    const raw = String(value ?? "").trim();
    if (!raw) return "";
    try {
      SQL_FORMAT_TOKEN.lastIndex = 0;
      const tokens = [...raw.matchAll(SQL_FORMAT_TOKEN)]
        .map((match) => match[0])
        .filter((token) => !/^\s+$/.test(token));
      const lines = [];
      const parens = [];
      const listClauses = new Set(["with", "select", "from", "group", "order", "returning", "values", "set"]);
      let line = "";
      let lineIndent = 0;
      let pendingIndent = null;
      let indent = 0;
      let clause = "";
      let clauseDepth = 0;
      let previous = "";

      const flush = () => {
        const text = line.trim();
        if (text) lines.push(`${"  ".repeat(Math.max(0, lineIndent))}${text}`);
        line = "";
      };
      const append = (text, spaced = true, level = null) => {
        if (!line) {
          lineIndent = level ?? pendingIndent ?? indent;
          pendingIndent = null;
        }
        if (spaced && line && !line.endsWith(" ")) line += " ";
        line += text;
      };
      const phraseAt = (index) => SQL_FORMAT_PHRASES.find(([words]) => (
        words.every((word, offset) => tokens[index + offset]?.toLowerCase() === word)
      ));

      for (let index = 0; index < tokens.length; index += 1) {
        const token = tokens[index];
        const lower = token.toLowerCase();
        const phrase = /^[a-z_]/i.test(token) ? phraseAt(index) : null;
        const phraseWords = phrase?.[0] || [lower];
        const phraseKind = phrase?.[1] || SQL_MAJOR_CLAUSES.get(lower);
        if (phraseKind) {
          flush();
          const text = phraseWords.map((word) => word.toUpperCase()).join(" ");
          append(text, false, indent);
          clause = phraseKind;
          clauseDepth = parens.length;
          index += phraseWords.length - 1;
          previous = phraseWords.at(-1);
          continue;
        }

        if (lower === "on" && parens.length === clauseDepth) {
          flush();
          append("ON", false, indent + 1);
          clause = "on";
          previous = lower;
          continue;
        }
        if (["and", "or"].includes(lower) && ["where", "having", "qualify", "on"].includes(clause)) {
          flush();
          append(lower.toUpperCase(), false, indent + 1);
          previous = lower;
          continue;
        }
        if (["when", "else"].includes(lower)) {
          flush();
          append(lower.toUpperCase(), false, indent + 1);
          previous = lower;
          continue;
        }
        if (lower === "end") {
          flush();
          append("END", false, indent);
          previous = lower;
          continue;
        }

        if (token === "(") {
          const next = tokens[index + 1]?.toLowerCase();
          const block = ["select", "with", "where"].includes(next);
          const functionLike = /^[a-z_][a-z0-9_$]*$/i.test(previous)
            && (!SQL_KEYWORDS.has(previous) || ["cast", "extract", "overlay", "position", "substring", "trim"].includes(previous));
          if (!line) append("(", false);
          else line = `${line.trimEnd()}${functionLike ? "" : " "}(`;
          const closeIndent = lineIndent;
          parens.push({ block, indent, closeIndent, clause, clauseDepth });
          if (block) {
            flush();
            indent = closeIndent + 1;
            clause = "";
            clauseDepth = parens.length;
          }
          previous = "(";
          continue;
        }
        if (token === ")") {
          const frame = parens.pop();
          if (frame?.block) {
            flush();
            indent = frame.indent;
            clause = frame.clause;
            clauseDepth = frame.clauseDepth;
            append(")", false, frame.closeIndent);
          } else if (!line) append(")", false, indent);
          else line = `${line.trimEnd()})`;
          previous = ")";
          continue;
        }
        if (token === ",") {
          line = `${line.trimEnd()},`;
          if (listClauses.has(clause) && parens.length === clauseDepth) {
            flush();
            pendingIndent = indent + 1;
          }
          previous = token;
          continue;
        }
        if (token === ".") {
          line = `${line.trimEnd()}.`;
          previous = token;
          continue;
        }
        if (token === ";") {
          line = `${line.trimEnd()};`;
          flush();
          previous = token;
          continue;
        }
        if (token === "::") {
          line = `${line.trimEnd()}::`;
          previous = token;
          continue;
        }
        if (/^(?:--|\/\*)/.test(token)) {
          if (line) append(token);
          else append(token, false, indent);
          flush();
          previous = "comment";
          continue;
        }

        const rendered = SQL_KEYWORDS.has(lower) ? lower.toUpperCase() : token;
        const noSpace = !line || previous === "(" || previous === "." || previous === "::";
        append(rendered, !noSpace);
        previous = lower;
      }
      flush();
      return lines.join("\n") || raw;
    } catch {
      return raw;
    }
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
    const rowValues = Object.fromEntries(cells.map((rowCell, index) => [
      boundedText(headers[index]?.textContent, 160) || `column_${index + 1}`,
      boundedText(rowCell.textContent, 400),
    ]));
    return {
      row_index: Math.max(0, rows.indexOf(row)),
      column_index: Math.max(0, columnIndex),
      column_header: boundedText(headers[columnIndex]?.textContent, 160),
      cell_text: boundedText(cell.textContent, 400),
      row: rowValues,
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
    const semanticObject = source.closest("[data-rvbbit-object]");
    if (
      semanticObject
      && semanticObject !== document.body
      && semanticObject !== document.documentElement
    ) return semanticObject;
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

  function semanticEntries() {
    try {
      const entries = dashboard.semanticObjects?.();
      return Array.isArray(entries) ? entries.filter((entry) => entry?.id) : [];
    } catch {
      return [];
    }
  }

  function semanticBoundElements() {
    const found = new Set();
    semanticEntries().forEach((semanticObject) => {
      (semanticObject.bindings || []).forEach((binding) => {
        try {
          if (binding.selector) {
            document.querySelectorAll(binding.selector).forEach((node) => found.add(node));
          }
          if (binding.element_id) {
            const node = document.getElementById(binding.element_id);
            if (node) found.add(node);
          }
          if (binding.name) {
            [...document.getElementsByName(binding.name)].forEach((node) => found.add(node));
          }
        } catch {
          // A stale selector should not disable the rest of the semantic map.
        }
      });
      found.forEach((node) => {
        if (
          (semanticObject.bindings || []).some((binding) => {
            try {
              if (binding.selector && node.matches(binding.selector)) return true;
              if (binding.element_id && node.id === binding.element_id) return true;
              if (binding.name && node.getAttribute("name") === binding.name) return true;
            } catch {
              return false;
            }
            return false;
          })
        ) node.setAttribute("data-rvbbit-object", semanticObject.id);
      });
    });
    return found;
  }

  function semanticObjectForElement(element) {
    if (!(element instanceof Element)) return null;
    const bound = element.closest("[data-rvbbit-object]");
    const objectId = bound?.getAttribute("data-rvbbit-object");
    if (objectId) {
      try {
        return dashboard.semanticObject?.(objectId, bound)
          || semanticEntries().find((entry) => entry.id === objectId)
          || null;
      } catch {
        return null;
      }
    }
    const matched = semanticEntries().find(
      (semanticObject) => semanticBindingForElement(semanticObject, element),
    );
    return matched
      ? (dashboard.semanticObject?.(matched.id, element) || matched)
      : null;
  }

  function semanticBindingForElement(semanticObject, element) {
    const bindings = semanticObject?.bindings || [];
    return bindings.find((binding) => {
      try {
        if (binding.selector && [...document.querySelectorAll(binding.selector)].some(
          (node) => node === element || node.contains(element),
        )) return true;
        if (binding.element_id) {
          const node = document.getElementById(binding.element_id);
          if (node && (node === element || node.contains(element))) return true;
        }
        if (binding.name) {
          return [...document.getElementsByName(binding.name)].some(
            (node) => node === element || node.contains(element),
          );
        }
      } catch {
        return false;
      }
      return false;
    }) || null;
  }

  function semanticSourceValue(source, element, target) {
    const raw = String(source || "").trim();
    if (!raw) return undefined;
    if (raw.startsWith("$element.data.")) {
      const key = raw.slice("$element.data.".length).replaceAll("_", "-");
      const owner = element.closest?.(`[data-${safeCss(key)}]`);
      return owner?.getAttribute?.(`data-${key}`) ?? undefined;
    }
    if (raw.startsWith("$element.attr.")) {
      const key = raw.slice("$element.attr.".length);
      if (!key || SENSITIVE.test(key)) return undefined;
      const owner = element.closest?.(`[${safeCss(key)}]`);
      return owner?.getAttribute?.(key) ?? undefined;
    }
    if (raw.startsWith("$element.text_number.")) {
      const index = Number(raw.slice("$element.text_number.".length));
      if (!Number.isInteger(index) || index < 0) return undefined;
      const text = boundedText(
        element.textContent || element.getAttribute?.("title"),
        1000,
      );
      const values = text.match(
        /[~≈<>≤≥]?\s*[+-]?\s*(?:[$€£¥]\s*)?\(?\d[\d,]*(?:\.\d+)?\)?(?:\s*%)?(?:\s*[kmbt])?/ig,
      ) || [];
      return values[index]?.trim();
    }
    if (raw.startsWith("$selection.data.")) {
      const key = `data-${raw.slice("$selection.data.".length).replaceAll("_", "-")}`;
      return target.data?.[key];
    }
    if (raw === "$selection.chart.data_label") return target.chart?.data_label;
    if (raw === "$selection.chart.dataset_label") return target.chart?.dataset_label;
    if (raw === "$selection.chart.value") return target._chart?.raw ?? target.chart?.value;
    if (raw === "$selection.table.cell_text") return target.table?.cell_text;
    if (raw.startsWith("$selection.table.row.")) {
      const key = raw.slice("$selection.table.row.".length);
      return target.table?.row?.[key];
    }
    if (raw.startsWith("$")) return undefined;
    try {
      const node = document.querySelector(raw);
      if (!node) return undefined;
      if (node.matches("input[type='checkbox'],input[type='radio']")) return Boolean(node.checked);
      if (node.matches("input,select,textarea")) return node.value;
      return boundedText(node.textContent, 1000);
    } catch {
      return undefined;
    }
  }

  function semanticSelectionForElement(semanticObject, element, target) {
    if (!semanticObject) return null;
    const runtime = (
      semanticObject.runtime
      && typeof semanticObject.runtime === "object"
      && !Array.isArray(semanticObject.runtime)
    ) ? semanticObject.runtime : {};
    const manifestBinding = semanticBindingForElement(semanticObject, element) || {};
    const context = {
      ...(
        manifestBinding.context
        && typeof manifestBinding.context === "object"
        && !Array.isArray(manifestBinding.context)
          ? manifestBinding.context
          : {}
      ),
      ...(
        runtime.context
        && typeof runtime.context === "object"
        && !Array.isArray(runtime.context)
          ? runtime.context
          : {}
      ),
    };
    Object.entries(semanticObject.parameters || {}).forEach(([name, spec]) => {
      const sourced = semanticSourceValue(spec?.source, element, target);
      if (sourced !== undefined) context[name] = sourced;
      else if (!(name in context) && spec && Object.hasOwn(spec, "default")) context[name] = spec.default;
    });
    const sourcedValue = semanticSourceValue(manifestBinding.value_source, element, target);
    const fallbackValue = (
      target._chart?.raw
      ?? target.table?.cell_text
      ?? target.data?.["data-value"]
      ?? target.text
      ?? ""
    );
    return {
      id: semanticObject.id,
      definition_hash: semanticObject.definition_hash || null,
      context,
      rendered_value: runtime.value ?? sourcedValue ?? fallbackValue,
    };
  }

  async function pollSemanticEnrichment() {
    window.clearTimeout(semanticPollTimer);
    const status = dashboard.manifest?.semantic_enrichment?.status;
    if (!["pending", "running"].includes(status)) return;
    try {
      const version = dashboard.version ? `?version=${encodeURIComponent(dashboard.version)}` : "";
      const result = await fetchJson(
        `/api/d/${encodeURIComponent(dashboard.slug)}/semantic-enrichment${version}`,
      );
      if (["ready", "partial"].includes(result.status) && result.manifest) {
        dashboard.replaceSemanticManifest?.(result.manifest);
        scheduleCandidateHighlights();
        return;
      }
      if (result.status === "failed" || result.status === "disabled") return;
    } catch {
      // Compilation is optional metadata; a transient poll failure never affects the dashboard.
    }
    semanticPollTimer = window.setTimeout(pollSemanticEnrichment, 3000);
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
      "display:block",
      "position:fixed",
      "z-index:2147483000",
      "right:max(18px,env(safe-area-inset-right))",
      "bottom:max(18px,env(safe-area-inset-bottom))",
      "width:max-content",
      "height:max-content",
      "pointer-events:none",
      "color-scheme:dark",
    ].join(";");
    // Dashboard styles can be arbitrarily slow (or contain broad custom-element
    // rules), so keep the host completely out of the paint tree until its own
    // shadow stylesheet is ready. The priority prevents an authored dashboard
    // rule from accidentally revealing the raw Lens controls during startup.
    host.style.setProperty("visibility", "hidden", "important");
    host.setAttribute("aria-hidden", "true");
    const root = host.attachShadow({ mode: "open" });
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/theme/artifact-lens.css";
    let stylesheetReady = false;
    let stylesheetFailed = false;
    let lensReady = false;
    let revealLensWhenReady = () => {};
    stylesheet.addEventListener("load", () => {
      stylesheetReady = true;
      revealLensWhenReady();
    }, { once: true });
    stylesheet.addEventListener("error", () => {
      stylesheetFailed = true;
      host.remove();
    }, { once: true });
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
          <small>Explore retained data time or understand the business objects on this surface.</small>
        </header>
        <nav class="tabs" aria-label="Artifact lens modes">
          <button type="button" class="tab active" data-view="time">Data time</button>
          <button type="button" class="tab" data-view="trace">Objects</button>
          <button type="button" class="query-browser" aria-label="Browse dashboard query data"
            title="Browse dashboard query data" disabled>
            <span>Query data</span><small class="query-count">0</small>
          </button>
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
            <strong>Select a value you want to understand.</strong>
            <p>Named values explain what they mean, preserve the dashboard context, and can recreate themselves from warehouse data.</p>
            <span class="pick-status"><i aria-hidden="true"></i>Selection active</span>
          </div>
          <div class="trace-loading loading" hidden role="status">
            <i aria-hidden="true"></i>
            <span>Following the evidence…</span>
          </div>
          <div class="trace-result" hidden></div>
        </div>
        <footer>Versioned semantic map · dashboard code stays independent</footer>
      </section>
      <aside class="query-drawer" data-side="left" aria-label="Query result" aria-hidden="true">
        <header class="query-drawer-head">
          <div>
            <span class="eyebrow">Live result set</span>
            <strong class="query-drawer-title">Recreated value</strong>
            <small class="query-drawer-meta">Authenticated · read-only warehouse evidence</small>
          </div>
          <div class="query-drawer-actions">
            <button class="query-sql-toggle" type="button" aria-pressed="false"
              aria-label="View query SQL" title="View query SQL" hidden>SQL</button>
            <button class="query-refresh" type="button" aria-label="Run query again" title="Run query again">↻</button>
            <button class="query-drawer-close" type="button" aria-label="Close query result">×</button>
          </div>
        </header>
        <label class="query-source-picker" hidden>
          <span>Dashboard result set</span>
          <select class="query-source" aria-label="Choose dashboard query result"></select>
        </label>
        <div class="query-drawer-content">
          <div class="query-result-view">
            <div class="query-drawer-loading" hidden role="status">
              <i aria-hidden="true"></i>
              <span>Recreating from warehouse data…</span>
            </div>
            <div class="query-drawer-error" hidden></div>
            <div class="query-table-wrap" hidden>
              <table class="query-table"></table>
            </div>
            <div class="query-empty" hidden>No rows returned.</div>
          </div>
          <section class="query-sql-view" aria-label="Executed SQL" hidden>
            <div class="query-sql-toolbar">
              <span>Executed SQL</span>
              <button class="query-sql-copy" type="button">Copy SQL</button>
            </div>
            <pre tabindex="0"><code class="query-sql-code"></code></pre>
          </section>
        </div>
        <footer class="query-drawer-foot">
          <span class="query-row-status">Ready to run</span>
          <div class="query-drawer-foot-actions">
            <button class="query-more" type="button" hidden>Show 100 more</button>
            <button class="query-analyze" type="button" hidden>Ask Calliope</button>
          </div>
        </footer>
      </aside>
      <button class="trigger" type="button" aria-expanded="false" aria-label="Open artifact lens"
        title="Click to open · drag to move">
        <span class="clock" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M12 7v5l3 2M5.6 5.7A8.5 8.5 0 1 1 3.5 12"/>
            <path d="M3.5 4.5v5h5"/>
          </svg>
        </span>
        <span class="trigger-copy">
          <b>Artifact lens</b>
          <small>${activeAsOf ? formatPoint(activeAsOf, false) : "Live · objects"}</small>
        </span>
        <i aria-hidden="true"></i>
      </button>`;
    root.appendChild(shell);
    document.body.appendChild(host);
    if (stylesheetFailed) {
      host.remove();
      return;
    }

    const panel = root.querySelector(".panel");
    const panelHeader = panel.querySelector("header");
    const trigger = root.querySelector(".trigger");
    const close = root.querySelector(".close");
    const tabs = [...root.querySelectorAll(".tab")];
    const queryBrowser = root.querySelector(".query-browser");
    const queryCount = root.querySelector(".query-count");
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
    const pickStatus = root.querySelector(".pick-status");
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
    const queryResultView = root.querySelector(".query-result-view");
    const queryTableWrap = root.querySelector(".query-table-wrap");
    const queryTable = root.querySelector(".query-table");
    const queryEmpty = root.querySelector(".query-empty");
    const querySqlToggle = root.querySelector(".query-sql-toggle");
    const querySqlView = root.querySelector(".query-sql-view");
    const querySqlCode = root.querySelector(".query-sql-code");
    const querySqlCopy = root.querySelector(".query-sql-copy");
    const queryRowStatus = root.querySelector(".query-row-status");
    const queryMore = root.querySelector(".query-more");
    const queryAnalyze = root.querySelector(".query-analyze");
    const queryRefresh = root.querySelector(".query-refresh");
    const queryDrawerClose = root.querySelector(".query-drawer-close");
    const querySourcePicker = root.querySelector(".query-source-picker");
    const querySource = root.querySelector(".query-source");
    let queryResult = null;
    let queryContext = null;
    let queryVisibleRows = RESULT_BATCH_SIZE;
    let queryRunning = false;
    let dragState = null;
    let suppressTriggerClickUntil = 0;

    function setHostPosition(left, top) {
      host.style.left = `${Math.round(left)}px`;
      host.style.top = `${Math.round(top)}px`;
      host.style.right = "auto";
      host.style.bottom = "auto";
    }

    function lensBounds() {
      const rects = [shell.getBoundingClientRect()];
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
      const handle = event.currentTarget;
      const draggingTrigger = handle === trigger;
      if (
        event.button !== 0
        || (!draggingTrigger && event.target.closest("button,a,input,select,textarea"))
      ) return;
      const anchorRect = shell.getBoundingClientRect();
      if (!Number.isFinite(Number.parseFloat(host.style.left))) {
        setHostPosition(anchorRect.left, anchorRect.top);
      }
      dragState = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startLeft: Number.parseFloat(host.style.left),
        startTop: Number.parseFloat(host.style.top),
        bounds: lensBounds(),
        handle,
        draggingTrigger,
        moved: false,
      };
      handle.setPointerCapture?.(event.pointerId);
      shell.dataset.dragging = "true";
      event.preventDefault();
    }

    function moveLens(event) {
      if (!dragState || event.pointerId !== dragState.pointerId) return;
      const margin = window.innerWidth <= 540 ? 8 : 12;
      let dx = event.clientX - dragState.startX;
      let dy = event.clientY - dragState.startY;
      if (!dragState.moved && Math.hypot(dx, dy) < 4) return;
      dragState.moved = true;
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
      const completedDrag = dragState;
      completedDrag.handle.releasePointerCapture?.(event.pointerId);
      dragState = null;
      shell.dataset.dragging = "false";
      if (completedDrag.moved) {
        persistLensPosition();
        if (completedDrag.draggingTrigger) {
          suppressTriggerClickUntil = performance.now() + 350;
        }
      }
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
      if (element.matches("[data-rvbbit-object]")) return "semantic";
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
      if (!pickerEnabled()) return;
      semanticBoundElements();
      const index = traceValueIndex();
      const candidates = new Set(document.querySelectorAll([
        "[data-rvbbit-object]", "td", "th", "canvas", "svg",
        "[data-field]", "[data-metric]", "[data-series]", "[data-dimension]",
      ].join(",")));
      if (!semanticEntries().length && !index.values.size && !index.rowCounts.size) return;
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
          (
            left.confidence === right.confidence
              ? 0
              : left.confidence === "semantic"
                ? -1
                : right.confidence === "semantic"
                  ? 1
                  : left.confidence === "exact"
                    ? -1
                    : 1
          )
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
      if (!pickerEnabled()) {
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
        stopPicker();
        loadTimeline();
      } else {
        if (shell.dataset.open === "true" && !pickerActive) startPicker();
        scheduleCandidateHighlights();
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
        if (!pickerActive) startPicker();
        scheduleCandidateHighlights();
      }
      if (!open) {
        // Closing the Lens is a hard interaction boundary. Always tear down
        // capture listeners and every visual selection affordance, even if a
        // prior async inspection or state transition left pickerActive stale.
        stopPicker();
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
      if (!pickerEnabled() || !element?.isConnected) {
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
      if (!pickerEnabled()) return;
      window.cancelAnimationFrame(hoverFrame);
      hoverFrame = window.requestAnimationFrame(() => {
        if (!pickerEnabled()) return;
        const element = meaningfulElement(event);
        if (element) positionOutline(element);
      });
    }

    async function inspectElement(element, event) {
      if (!pickerEnabled() || inspectionBusy) return;
      inspectionBusy = true;
      closeQueryDrawer();
      queryResult = null;
      queryContext = null;
      pickedElement = element;
      positionOutline(element, true);
      const target = describeElement(element, event);
      const semanticObject = semanticObjectForElement(element);
      const semanticSelection = semanticSelectionForElement(
        semanticObject, element, target,
      );
      const meaning = semanticObject?.meaning || {};
      const semanticEvaluator = semanticObject?.evaluator || {};
      const binding = semanticObject
        ? {
            kind: "value",
            confidence: "semantic",
            field: semanticEvaluator.value_column || "",
            label: meaning.label || target.label,
            value: formatValue(semanticSelection?.rendered_value),
          }
        : resolveBinding(target);
      const cleanTarget = publicTarget(target);
      const cleanBinding = publicBinding(binding);
      setOpen(true);
      setView("trace");
      traceIntro.hidden = true;
      traceResult.hidden = true;
      traceLoading.hidden = false;
      pickerHint.textContent = "Click another highlighted object to inspect it · Esc to close";
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
              trace: semanticObject ? {} : tracePayload(binding),
              semantic_object: semanticSelection,
              as_of: activeAsOf || null,
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
            <small>Click another highlighted object to inspect it.</small>
          </div>`;
        traceResult.hidden = false;
      } finally {
        inspectionBusy = false;
        traceLoading.hidden = true;
        scheduleCandidateHighlights();
      }
    }

    function onPickerClick(event) {
      if (!pickerEnabled()) return;
      const element = meaningfulElement(event);
      if (!element) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (inspectionBusy || queryRunning) return;
      inspectElement(element, event);
    }

    function onPickerKey(event) {
      if (event.key !== "Escape" || !pickerEnabled()) return;
      event.preventDefault();
      if (shell.dataset.drawerOpen === "true") {
        closeQueryDrawer();
        return;
      }
      setOpen(false);
      trigger.focus();
    }

    function pickerEnabled() {
      return (
        pickerActive
        && shell.dataset.open === "true"
        && shell.dataset.view === "trace"
      );
    }

    function startPicker() {
      if (
        pickerActive
        || shell.dataset.open !== "true"
        || shell.dataset.view !== "trace"
      ) return;
      semanticBoundElements();
      pickerActive = true;
      shell.dataset.picking = "true";
      pickStatus.dataset.active = "true";
      pickerHint.hidden = shell.dataset.drawerOpen === "true";
      pickerHint.textContent = currentInspection
        ? "Click another highlighted object to inspect it · Esc to close"
        : "Pick a highlighted business value, chart point, table cell, or visual object · Esc to close";
      document.documentElement.classList.add("rvbbit-artifact-lens-picking");
      document.addEventListener("pointermove", onPickerMove, true);
      document.addEventListener("click", onPickerClick, true);
      document.addEventListener("keydown", onPickerKey, true);
    }

    function stopPicker() {
      pickerActive = false;
      shell.dataset.picking = "false";
      pickStatus.dataset.active = "false";
      pickerHint.hidden = true;
      document.documentElement.classList.remove("rvbbit-artifact-lens-picking");
      document.removeEventListener("pointermove", onPickerMove, true);
      document.removeEventListener("click", onPickerClick, true);
      document.removeEventListener("keydown", onPickerKey, true);
      window.cancelAnimationFrame(hoverFrame);
      window.clearTimeout(candidateTimer);
      pickedElement = null;
      outline.hidden = true;
      outline.dataset.selected = "false";
      candidateLayer.replaceChildren();
    }

    function openQueryDrawer() {
      shell.dataset.drawerOpen = "true";
      queryDrawer.setAttribute("aria-hidden", "false");
      pickerHint.hidden = true;
      positionQueryDrawer();
      window.requestAnimationFrame(positionQueryDrawer);
      scheduleCandidateHighlights();
    }

    function closeQueryDrawer() {
      setQuerySqlVisible(false);
      shell.dataset.drawerOpen = "false";
      queryDrawer.setAttribute("aria-hidden", "true");
      if (
        pickerActive
        && shell.dataset.open === "true"
        && shell.dataset.view === "trace"
      ) pickerHint.hidden = false;
      scheduleCandidateHighlights();
    }

    function setQuerySqlVisible(visible) {
      const sql = String(queryContext?.sql || "").trim();
      const formatted = formatSql(sql);
      const show = Boolean(visible && sql);
      shell.dataset.sqlView = String(show);
      querySqlToggle.hidden = !sql;
      querySqlToggle.disabled = !sql;
      querySqlToggle.setAttribute("aria-pressed", String(show));
      querySqlToggle.setAttribute("aria-label", show ? "Show query result" : "View query SQL");
      querySqlToggle.title = show ? "Show query result" : "View query SQL";
      queryResultView.hidden = show;
      querySqlView.hidden = !show;
      querySqlCopy.textContent = "Copy SQL";
      if (formatted && querySqlCode.textContent !== formatted) {
        querySqlCode.innerHTML = highlightSql(formatted);
      }
      if (!sql) querySqlCode.replaceChildren();
      positionQueryDrawer();
    }

    async function copyQuerySql() {
      const sql = String(queryContext?.sql || "").trim();
      if (!sql) return;
      try {
        await navigator.clipboard.writeText(formatSql(sql));
        querySqlCopy.textContent = "Copied";
      } catch {
        querySqlCopy.textContent = "Copy failed";
      }
      window.setTimeout(() => {
        if (querySqlCopy.isConnected) querySqlCopy.textContent = "Copy SQL";
      }, 1400);
    }

    function dashboardQueryEntries() {
      const seen = new Set();
      return traceEntries().flatMap((trace, traceIndex) => {
        const dedupeKey = `${trace.query_hash || trace.id || traceIndex}:${trace.as_of || "latest"}`;
        if (!trace.sql || seen.has(dedupeKey)) return [];
        seen.add(dedupeKey);
        return [{
          key: String(trace.id || dedupeKey),
          trace,
          traceIndex,
        }];
      });
    }

    function dashboardQueryLabel(entry, index) {
      const columns = resultColumns(entry.trace).map((column) => column.name);
      const fieldLabel = columns.length
        ? `${columns.slice(0, 3).join(", ")}${columns.length > 3 ? "…" : ""}`
        : `Query ${index + 1}`;
      const rows = Array.isArray(entry.trace.rows) ? entry.trace.rows.length : 0;
      const returned = Math.max(rows, Number(entry.trace.row_count) || 0);
      return `${index + 1}. ${fieldLabel} · ${returned}${entry.trace.truncated ? "+" : ""} row${returned === 1 ? "" : "s"}`;
    }

    function populateQuerySource(entries, selectedKey = "") {
      querySource.replaceChildren(...entries.map((entry, index) => {
        const option = document.createElement("option");
        option.value = entry.key;
        option.textContent = dashboardQueryLabel(entry, index);
        return option;
      }));
      if (selectedKey && entries.some((entry) => entry.key === selectedKey)) {
        querySource.value = selectedKey;
      }
    }

    function syncQueryBrowser() {
      const entries = dashboardQueryEntries();
      queryCount.textContent = String(entries.length);
      queryBrowser.disabled = entries.length === 0;
      queryBrowser.title = entries.length
        ? `Browse ${entries.length} dashboard query result${entries.length === 1 ? "" : "s"}`
        : "Dashboard query results are still loading";
      if (queryContext?.kind === "dashboard") {
        populateQuerySource(entries, queryContext.key);
      }
    }

    function openDashboardQuery(key = "") {
      const entries = dashboardQueryEntries();
      if (!entries.length) return;
      const index = Math.max(0, entries.findIndex((entry) => entry.key === key));
      const entry = entries[index] || entries[0];
      const trace = entry.trace;
      const columns = resultColumns(trace).map((column) => column.name);
      queryContext = {
        kind: "dashboard",
        key: entry.key,
        sql: String(trace.sql || "").trim(),
        as_of: trace.as_of || activeAsOf || null,
        origin: "artifact-lens",
        query_hash: trace.query_hash || null,
        engine: trace.engine || null,
        title: columns.length
          ? columns.slice(0, 3).join(" · ")
          : `Dashboard query ${index + 1}`,
      };
      setQuerySqlVisible(false);
      populateQuerySource(entries, entry.key);
      querySourcePicker.hidden = false;
      queryRefresh.disabled = !queryContext.sql;
      renderQueryResult({ ...trace, captured: true });
      openQueryDrawer();
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
      const reported = Math.max(returned, Number(queryResult.row_count) || 0);
      const shown = visible.length;
      const capped = queryResult.truncated ? "+" : "";
      queryRowStatus.textContent = rows.length
        ? queryResult.captured && reported > returned
          ? `Showing ${shown} of ${returned} captured rows · query returned ${reported}${capped}`
          : `Showing ${shown} of ${returned}${capped} returned rows`
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
      const context = queryContext || {};
      const provenance = currentInspection?.provenance || {};
      const semanticLabel = currentInspection?.semantic_object?.meaning?.label;
      const reported = Math.max(rows.length, Number(result.row_count) || 0);
      const rowLabel = result.captured && reported > rows.length
        ? `${rows.length} captured of ${reported}${result.truncated ? "+" : ""} rows`
        : `${rows.length}${result.truncated ? "+" : ""} row${rows.length === 1 ? "" : "s"}`;
      queryDrawerTitle.textContent = context.title
        || semanticLabel
        || currentInspection?.selection?.label
        || provenance.query_hash
        || "Warehouse evidence";
      queryDrawerMeta.textContent = [
        result.engine || context.engine || provenance.engine || "read-only",
        rowLabel,
        `${columns.length} column${columns.length === 1 ? "" : "s"}`,
        Number.isFinite(Number(result.elapsed_ms)) ? `${Number(result.elapsed_ms)} ms` : "",
        result.as_of_applied ? `AS-OF ${formatPoint(result.as_of_applied)}` : "latest data",
      ].filter(Boolean).join(" · ");
      queryDrawerLoading.hidden = true;
      queryDrawerError.hidden = true;
      queryAnalyze.hidden = !(
        context.sql
        && (dashboard.calliope_enabled || currentInspection?.calliope_enabled)
      );
      queryAnalyze.disabled = false;
      queryAnalyze.textContent = "Ask Calliope";
      renderQueryRows();
    }

    function activateInspectionQuery() {
      const provenance = currentInspection?.provenance || {};
      queryContext = {
        kind: "inspection",
        sql: String(provenance.sql || "").trim(),
        as_of: provenance.as_of || activeAsOf || null,
        origin: currentInspection?.semantic_object ? "semantic-lens" : "artifact-lens",
        query_hash: provenance.query_hash || null,
        engine: provenance.engine || null,
        title: currentInspection?.semantic_object?.meaning?.label
          || currentInspection?.selection?.label
          || "Warehouse evidence",
      };
      setQuerySqlVisible(false);
      querySourcePicker.hidden = true;
      return queryContext;
    }

    async function runInspectionQuery(button = null) {
      const context = button || !queryContext ? activateInspectionQuery() : queryContext;
      const sql = String(context?.sql || "").trim();
      if (!sql || queryRunning) return;
      queryRunning = true;
      root.querySelectorAll(".query-run").forEach((node) => {
        node.disabled = true;
        node.textContent = "Running…";
      });
      queryRefresh.disabled = true;
      queryDrawerTitle.textContent = context.title || "Warehouse evidence";
      queryDrawerMeta.textContent = context.as_of
        ? `Preparing retained snapshot · ${formatPoint(context.as_of)}`
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
              as_of: context.as_of || null,
              origin: context.origin || "artifact-lens",
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
          node.textContent = currentInspection?.semantic_object
            ? "Open recreated result"
            : "Run query";
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

    function formatSemanticValue(value, display = {}, unit = "") {
      if (value === null || value === undefined || value === "") return "—";
      let rendered = value;
      if (typeof value === "number") {
        const decimals = Number(display.decimals);
        rendered = new Intl.NumberFormat(undefined, {
          maximumFractionDigits: Number.isInteger(decimals)
            ? Math.max(0, Math.min(decimals, 12))
            : 2,
          minimumFractionDigits: Number.isInteger(decimals)
            ? Math.max(0, Math.min(decimals, 12))
            : 0,
        }).format(value);
      } else {
        rendered = boundedText(value, 240);
      }
      const prefix = boundedText(display.prefix, 16);
      const suffix = boundedText(display.suffix, 24);
      const unitSuffix = unit && !prefix && !suffix ? ` ${boundedText(unit, 40)}` : "";
      return `${prefix}${rendered}${suffix}${unitSuffix}`;
    }

    function renderSemanticInspection(data) {
      const semanticObject = data.semantic_object || {};
      const meaning = semanticObject.meaning || {};
      const display = semanticObject.display || {};
      const replay = data.replay || {};
      const provenance = data.provenance || {};
      const comparison = data.comparison;
      const renderedValue = (
        replay.rendered_value
        ?? data.binding?.value
        ?? data.selection?.text
      );
      const watchable = Boolean(data.calliope_enabled && watchNumericValue(data) !== null);
      const replayStatuses = {
        verified: {
          label: "Verified",
          title: "Matches the dashboard",
          copy: "The warehouse independently recreated the value shown on this surface.",
        },
        recreated: {
          label: "Recreated",
          title: "Reproduced from warehouse data",
          copy: "The evaluator ran successfully; the dashboard did not expose a comparable raw value.",
        },
        mismatch: {
          label: "Changed",
          title: "The values do not currently agree",
          copy: "The rendered value and a fresh warehouse recreation differ. Filters, timing, or client-side logic may have changed.",
        },
        error: {
          label: "Unavailable",
          title: "Could not recreate this value",
          copy: replay.error || "The saved evaluator could not be run.",
        },
      };
      const replayState = replayStatuses[replay.status] || replayStatuses.recreated;
      const contextEntries = Object.entries(semanticObject.context || {});
      const context = contextEntries.length
        ? `<div class="semantic-context">
            <span>Dashboard context</span>
            <div>${contextEntries.map(([key, value]) => (
              `<i><b>${escapeHtml(String(key).replaceAll("_", " "))}</b>${escapeHtml(formatValue(value))}</i>`
            )).join("")}</div>
          </div>`
        : "";
      const formula = meaning.formula
        ? `<div class="semantic-definition">
            <span>How to read it</span>
            <p>${escapeHtml(meaning.formula)}</p>
          </div>`
        : "";
      const replayCard = `
        <div class="semantic-replay ${escapeHtml(replay.status || "recreated")}">
          <span>${escapeHtml(replayState.label)}</span>
          <strong>${escapeHtml(replayState.title)}</strong>
          <div>
            <i>
              <small>On dashboard</small>
              <b>${escapeHtml(formatSemanticValue(renderedValue, display, meaning.unit))}</b>
            </i>
            <em>↔</em>
            <i>
              <small>From warehouse</small>
              <b>${escapeHtml(formatSemanticValue(replay.value, display, meaning.unit))}</b>
            </i>
          </div>
          <p>${escapeHtml(replayState.copy)}</p>
        </div>`;
      const comparisonCard = comparison
        ? `<div class="evidence-card comparison-card">
            <span>Then → now</span>
            <div class="compare-values">
              <strong>${escapeHtml(formatSemanticValue(comparison.current, display, meaning.unit))}</strong>
              <i>→</i>
              <strong>${escapeHtml(formatSemanticValue(comparison.latest, display, meaning.unit))}</strong>
            </div>
            <small>${escapeHtml(formatPoint(comparison.as_of))}${comparison.delta ? ` · ${escapeHtml(formatDelta(comparison.delta))}` : ""}</small>
          </div>`
        : "";
      const sources = (data.sources || []).length
        ? data.sources.map(sourceCard).join("")
        : "";
      const technical = provenance.sql
        ? `<details class="evidence-card query-card technical-card">
            <summary>
              <span>Technical evidence</span>
              <strong>Reproducible warehouse definition</strong>
              <small>${escapeHtml(provenance.engine || "read-only")} · ${(provenance.tables || []).length} source${(provenance.tables || []).length === 1 ? "" : "s"} · definition ${escapeHtml(semanticObject.definition_hash || "")}</small>
            </summary>
            <pre>${escapeHtml(provenance.sql)}</pre>
          </details>`
        : "";
      const related = (data.related_artifacts || []).length
        ? `<div class="related">
            <span>Also built from this evidence</span>
            <div>${data.related_artifacts.map((artifact) => (
              `<a href="/d/${encodeURIComponent(artifact.slug)}" target="_blank" rel="noopener">${escapeHtml(artifact.name)}</a>`
            )).join("")}</div>
          </div>`
        : "";
      traceResult.innerHTML = `
        <div class="semantic-heading">
          <span class="confidence semantic">Named business object</span>
          <strong>${escapeHtml(meaning.label || semanticObject.id || "Business value")}</strong>
          ${meaning.description ? `<p>${escapeHtml(meaning.description)}</p>` : ""}
        </div>
        ${context}
        ${formula}
        <div class="evidence-stack">
          ${replayCard}
          ${comparisonCard}
          ${sources}
          ${technical}
        </div>
        ${related}
        <div class="trace-actions">
          ${provenance.sql ? '<button type="button" class="query-run">Open recreated result</button>' : ""}
          ${data.calliope_enabled ? '<button type="button" class="home-pin">Pin to Home</button>' : ""}
          ${watchable ? '<button type="button" class="watch-open">Watch this value</button>' : ""}
          ${data.calliope_enabled ? '<button type="button" class="trail-follow">Follow trail</button>' : ""}
          ${data.calliope_enabled ? '<button type="button" class="calliope-investigate">Ask Calliope</button>' : ""}
        </div>
        ${watchable ? '<section class="watch-panel" hidden></section>' : ""}
        <section class="lens-trail" hidden></section>`;
      traceResult.hidden = false;
      if (watchable) void loadSemanticWatches(false);
      window.requestAnimationFrame(constrainLens);
      scheduleCandidateHighlights();
    }

    function renderInspection(data) {
      currentTrail = null;
      trailHistory = [];
      trailRequest += 1;
      watchRequest += 1;
      currentWatches = null;
      watchPanelOpen = false;
      watchError = "";
      if (data.semantic_object) {
        renderSemanticInspection(data);
        return;
      }
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
          ${provenance.sql ? '<button type="button" class="query-run">Run query</button>' : ""}
          ${data.calliope_enabled ? '<button type="button" class="trail-follow">Follow trail</button>' : ""}
          ${data.calliope_enabled ? '<button type="button" class="calliope-investigate">Ask Calliope</button>' : ""}
        </div><section class="lens-trail" hidden></section>`;
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
        query_hash: queryContext?.query_hash || currentInspection?.provenance?.query_hash || null,
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

    function dashboardQueryInspection() {
      const title = queryContext?.title || "Dashboard query result";
      const width = Math.max(1, window.innerWidth);
      const height = Math.max(1, window.innerHeight);
      return {
        artifact: {
          slug: dashboard.slug,
          version: dashboard.version,
          name: document.title || dashboard.slug,
        },
        selection: {
          type: "artifact_element",
          label: title,
          selector: "body",
          tag: "body",
          text: "Dashboard query result",
          bounds: { x: 0, y: 0, width, height },
          viewport: {
            width,
            height,
            scroll_x: window.scrollX,
            scroll_y: window.scrollY,
            document_width: Math.max(width, document.documentElement.scrollWidth),
            document_height: Math.max(height, document.documentElement.scrollHeight),
          },
        },
        binding: {
          kind: "query",
          confidence: "exact",
          label: title,
          context: {
            query_hash: queryContext?.query_hash || null,
            as_of: queryContext?.as_of || activeAsOf || null,
            location_search: window.location.search,
          },
        },
        provenance: {
          confidence: "exact",
          sql: String(queryContext?.sql || "").trim(),
          query_hash: queryContext?.query_hash || null,
          engine: queryContext?.engine || queryResult?.engine || null,
          as_of: queryContext?.as_of || activeAsOf || null,
          source: "dashboard runtime query trace",
        },
        sources: [],
      };
    }

    async function launchCalliope(button, analyzeResult = false) {
      const preview = analyzeResult ? queryResultPreview() : null;
      if (analyzeResult && !preview) return;
      const baseInspection = analyzeResult && queryContext?.kind === "dashboard"
        ? dashboardQueryInspection()
        : currentInspection;
      if (!baseInspection) return;
      const idleLabel = "Ask Calliope";
      button.disabled = true;
      button.textContent = "Opening new session…";
      const pending = window.open("about:blank", "_blank");
      try {
        const inspection = analyzeResult
          ? { ...baseInspection, query_result: preview }
          : baseInspection;
        const data = await fetchJson("/api/calliope/investigations", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            slug: dashboard.slug,
            version: baseInspection.artifact?.version || dashboard.version,
            target: baseInspection.selection,
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

    async function pinSemanticObjectToHome(button) {
      const semanticObject = currentInspection?.semantic_object;
      const artifact = currentInspection?.artifact;
      if (!semanticObject?.id || !artifact?.slug || !artifact?.version) return;
      const idleLabel = "Pin to Home";
      button.disabled = true;
      button.textContent = "Pinning…";
      try {
        await fetchJson("/api/calliope/home/items", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            kind: "artifact_object",
            slug: artifact.slug,
            version: artifact.version,
            object_id: semanticObject.id,
            definition_hash: semanticObject.definition_hash,
            context: semanticObject.context || {},
            rendered_value: currentInspection?.replay?.rendered_value
              ?? currentInspection?.binding?.value
              ?? null,
          }),
        });
        button.textContent = "Pinned to Home";
        button.classList.add("pinned");
      } catch (error) {
        button.disabled = false;
        button.textContent = idleLabel;
        const note = document.createElement("span");
        note.className = "handoff-error";
        note.textContent = error instanceof Error ? error.message : String(error);
        button.parentElement?.appendChild(note);
      }
    }

    function watchNumericValue(data = currentInspection) {
      const raw = data?.replay?.value;
      if (raw === null || raw === undefined || typeof raw === "boolean") return null;
      const number = Number(String(raw).replaceAll(",", "").trim());
      return Number.isFinite(number) ? number : null;
    }

    function watchSuggestedThreshold(value) {
      if (!Number.isFinite(value) || value === 0) return 0;
      const target = value > 0 ? value * 0.9 : value * 1.1;
      const magnitude = Math.max(0, Math.floor(Math.log10(Math.abs(target))) - 2);
      const step = 10 ** magnitude;
      return Math.round(target / step) * step;
    }

    function watchNumber(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return "—";
      return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(number);
    }

    function watchHandle() {
      const handle = currentTrailHandle();
      return handle?.kind === "artifact_object" ? handle : null;
    }

    function watchHandleKey(handle = watchHandle()) {
      if (!handle) return "";
      const context = Object.entries(handle.context || {})
        .sort(([left], [right]) => left.localeCompare(right));
      return JSON.stringify([
        handle.slug, handle.version, handle.object_id,
        handle.definition_hash || "", context,
      ]);
    }

    function syncWatchButton() {
      const button = traceResult.querySelector(".watch-open");
      if (!button) return;
      const count = Array.isArray(currentWatches) ? currentWatches.length : 0;
      button.classList.toggle("watched", count > 0);
      button.textContent = count
        ? `${count} watch${count === 1 ? "" : "es"}`
        : "Watch this value";
    }

    function watchStatus(watch) {
      if (!watch.active) return { label: "Paused", tone: "paused" };
      if (watch.current?.error || watch.current?.status === "error") {
        return { label: "Read failed", tone: "error" };
      }
      if (watch.current?.status === "fail") return { label: "Attention", tone: "fail" };
      if (watch.current?.status === "pass") return { label: "Quiet", tone: "pass" };
      return { label: "Waiting", tone: "waiting" };
    }

    function renderWatchPanel() {
      const panel = traceResult.querySelector(".watch-panel");
      if (!panel) return;
      panel.hidden = !watchPanelOpen;
      if (!watchPanelOpen) return;
      const value = watchNumericValue();
      const semanticObject = currentInspection?.semantic_object || {};
      const meaning = semanticObject.meaning || {};
      const display = semanticObject.display || {};
      const threshold = watchSuggestedThreshold(value);
      const watches = Array.isArray(currentWatches) ? currentWatches : [];
      const list = watches.map((watch) => {
        const state = watchStatus(watch);
        const condition = watch.condition || {};
        const cadence = { fast: "every minute", normal: "every 15 minutes", slow: "hourly" }[
          condition.cadence
        ] || condition.cadence;
        const checks = Number(condition.consecutive_n || 1);
        return `<article class="watch-card ${escapeHtml(state.tone)}">
          <header>
            <i aria-hidden="true"></i>
            <div><strong>${escapeHtml(watch.name || meaning.label || "Semantic watch")}</strong>
            <small>${escapeHtml(state.label)} · ${escapeHtml(cadence || "scheduled")}</small></div>
          </header>
          <p>${escapeHtml(condition.copy || "crosses")} <b>${escapeHtml(watchNumber(condition.threshold))}</b>${
            meaning.unit ? ` ${escapeHtml(meaning.unit)}` : ""
          }${checks > 1 ? ` for ${checks} checks` : ""}</p>
          <div><span>Now <b>${escapeHtml(watchNumber(watch.current?.value))}</b></span>
            <button type="button" data-watch-check="${escapeHtml(watch.id)}">Check now</button>
            <button type="button" data-watch-active="${escapeHtml(watch.id)}" data-active="${watch.active}">${watch.active ? "Pause" : "Resume"}</button>
            <button type="button" class="danger" data-watch-delete="${escapeHtml(watch.id)}">Remove</button>
          </div>
          ${watch.current?.error ? `<small class="watch-card-error">${escapeHtml(watch.current.error)}</small>` : ""}
        </article>`;
      }).join("");
      const composer = currentWatches === null ? "" : `<details class="watch-composer"${watches.length ? "" : " open"}>
        <summary>${watches.length ? "Add another watch" : "Create a watch"}<span>Exact value · permission-aware replay</span></summary>
        <form class="watch-form">
          <label class="watch-name"><span>Name <i>optional</i></span><input name="name" maxlength="120" placeholder="${escapeHtml(meaning.label || "My watch")}"></label>
          <div class="watch-sentence">
            <span>Tell me when it</span>
            <select name="comparator" aria-label="Watch direction">
              <option value="below">falls to or below</option>
              <option value="above">rises to or above</option>
            </select>
            <input name="threshold" type="number" step="any" required value="${escapeHtml(threshold)}" aria-label="Threshold">
          </div>
          <div class="watch-options">
            <label><span>Check</span><select name="cadence">
              <option value="fast">Every minute</option>
              <option value="normal" selected>Every 15 minutes</option>
              <option value="slow">Hourly</option>
            </select></label>
            <label><span>Confirm for</span><select name="consecutive_n">
              <option value="1">1 check</option><option value="2">2 checks</option>
              <option value="3">3 checks</option><option value="5">5 checks</option>
            </select></label>
            <button type="submit">Create watch</button>
          </div>
          <p>Checks recreate this exact named value under your warehouse permissions. Calliope only records the observed number and meaningful changes.</p>
        </form>
      </details>`;
      panel.innerHTML = `<header class="watch-panel-head">
          <div><span>Semantic watch</span><strong>Keep an eye on ${escapeHtml(meaning.label || "this value")}</strong>
          <small>Current warehouse value · ${escapeHtml(formatSemanticValue(value, display, meaning.unit))}</small></div>
          <button type="button" class="watch-panel-close" aria-label="Close watch setup">×</button>
        </header>
        ${watchError ? `<div class="watch-error">${escapeHtml(watchError)}</div>` : ""}
        <div class="watch-list">
          ${currentWatches === null ? '<div class="watch-loading"><i></i><span>Reading your watches…</span></div>' : list}
        </div>
        ${composer}`;
      window.requestAnimationFrame(constrainLens);
    }

    async function loadSemanticWatches(open = watchPanelOpen) {
      const handle = watchHandle();
      if (!handle) return;
      if (open) watchPanelOpen = true;
      const key = watchHandleKey(handle);
      const request = ++watchRequest;
      if (watchPanelOpen) renderWatchPanel();
      try {
        const params = new URLSearchParams({
          slug: handle.slug,
          version: String(handle.version),
          object_id: handle.object_id,
        });
        const data = await fetchJson(`/api/calliope/watches?${params}`);
        if (request !== watchRequest || key !== watchHandleKey()) return;
        currentWatches = Array.isArray(data.watches) ? data.watches : [];
        watchError = "";
      } catch (error) {
        if (request !== watchRequest || key !== watchHandleKey()) return;
        currentWatches = [];
        watchError = error instanceof Error ? error.message : String(error);
      }
      syncWatchButton();
      if (watchPanelOpen) renderWatchPanel();
    }

    function openWatchPanel() {
      watchPanelOpen = true;
      watchError = "";
      renderWatchPanel();
      if (currentWatches === null) void loadSemanticWatches(true);
      traceView.scrollTo({ top: traceView.scrollHeight, behavior: "smooth" });
    }

    async function createSemanticWatch(form) {
      const handle = watchHandle();
      if (!handle) return;
      const submit = form.querySelector('[type="submit"]');
      const fields = new FormData(form);
      submit.disabled = true;
      submit.textContent = "Creating…";
      watchError = "";
      try {
        await fetchJson("/api/calliope/watches", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source: handle,
            name: fields.get("name"),
            comparator: fields.get("comparator"),
            threshold: fields.get("threshold"),
            cadence: fields.get("cadence"),
            consecutive_n: Number(fields.get("consecutive_n") || 1),
          }),
        });
        currentWatches = null;
        await loadSemanticWatches(true);
      } catch (error) {
        watchError = error instanceof Error ? error.message : String(error);
        renderWatchPanel();
      }
    }

    async function changeSemanticWatch(button, action) {
      const id = button.dataset.watchCheck
        || button.dataset.watchActive
        || button.dataset.watchDelete;
      if (!id) return;
      if (action === "delete" && !window.confirm("Remove this semantic watch?")) return;
      button.disabled = true;
      const original = button.textContent;
      button.textContent = action === "check" ? "Checking…" : action === "delete" ? "Removing…" : "Saving…";
      watchError = "";
      try {
        if (action === "check") {
          await fetchJson(`/api/calliope/watches/${encodeURIComponent(id)}/check`, { method: "POST" });
        } else if (action === "active") {
          await fetchJson(`/api/calliope/watches/${encodeURIComponent(id)}`, {
            method: "PATCH",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ active: button.dataset.active !== "true" }),
          });
        } else {
          await fetchJson(`/api/calliope/watches/${encodeURIComponent(id)}`, { method: "DELETE" });
        }
        currentWatches = null;
        await loadSemanticWatches(true);
      } catch (error) {
        button.disabled = false;
        button.textContent = original;
        watchError = error instanceof Error ? error.message : String(error);
        renderWatchPanel();
      }
    }

    function currentTrailHandle() {
      const semanticObject = currentInspection?.semantic_object;
      const artifact = currentInspection?.artifact || dashboard;
      if (semanticObject?.id && artifact?.slug) {
        return {
          kind: "artifact_object",
          slug: artifact.slug,
          version: artifact.version || dashboard.version,
          object_id: semanticObject.id,
          definition_hash: semanticObject.definition_hash,
          context: semanticObject.context || {},
        };
      }
      if (!artifact?.slug) return null;
      return {
        kind: "artifact",
        slug: artifact.slug,
        version: artifact.version || dashboard.version,
      };
    }

    function lensTrailStableValue(value) {
      if (Array.isArray(value)) return `[${value.map(lensTrailStableValue).join(",")}]`;
      if (value && typeof value === "object") {
        return `{${Object.keys(value).sort().map((key) => (
          `${JSON.stringify(key)}:${lensTrailStableValue(value[key])}`
        )).join(",")}}`;
      }
      return JSON.stringify(value);
    }

    function lensTrailHandleKey(handle) {
      return lensTrailStableValue(handle || {});
    }

    function lensTrailFrameIndex(handle) {
      const key = lensTrailHandleKey(handle);
      return trailHistory.findIndex((frame) => lensTrailHandleKey(frame.handle) === key);
    }

    function lensTrailSectionLabel(section) {
      return ({
        meaning: "What it means",
        artifacts: "Where it lives",
        knowledge: "What the company knows",
        data: "What it is built from",
      })[section] || "Related evidence";
    }

    function lensTrailRouteSummary(data) {
      const connections = data?.connections || [];
      const raw = data?.route_summary || {};
      const sections = { meaning: 0, artifacts: 0, knowledge: 0, data: 0 };
      connections.forEach((connection) => {
        const section = connection.section || "knowledge";
        sections[section] = (sections[section] || 0) + 1;
      });
      if (raw.sections) {
        Object.keys(sections).forEach((section) => {
          sections[section] = Number(raw.sections[section] || 0);
        });
      }
      return {
        resolved: Number(raw.resolved ?? connections.length),
        bounded: Boolean(raw.bounded),
        sections,
      };
    }

    function lensTrailConnectionContext(handle) {
      const key = lensTrailHandleKey(handle);
      const pathStep = lensTrailFrameIndex(handle);
      if (pathStep >= 0) {
        return { kind: "return", step: pathStep, label: `Returns to step ${pathStep + 1}` };
      }
      for (let step = 0; step < trailHistory.length - 1; step += 1) {
        const found = (trailHistory[step].data?.connections || []).some(
          (connection) => lensTrailHandleKey(connection.handle) === key,
        );
        if (found) {
          return { kind: "converges", step, label: `Also reachable from step ${step + 1}` };
        }
      }
      return null;
    }

    function lensTrailLoomMarkup() {
      if (!trailHistory.length) return "";
      const retained = trailHistory.reduce(
        (total, frame) => total + lensTrailRouteSummary(frame.data).resolved,
        0,
      );
      const steps = trailHistory.map((frame, index) => {
        const subject = frame.data?.subject || {};
        const summary = lensTrailRouteSummary(frame.data);
        const current = index === trailHistory.length - 1;
        const mixes = [
          ["meaning", "Meaning"], ["artifacts", "Places"],
          ["knowledge", "Knowledge"], ["data", "Data"],
        ].map(([section, label]) => (
          summary.sections[section] ? `<i>${label} ${summary.sections[section]}</i>` : ""
        )).join("");
        const link = index && frame.via
          ? `<span class="lens-trail-loom-link"><b>${escapeHtml(frame.via.relationship || "related to")}</b><i>↓</i></span>`
          : "";
        return `${link}<button type="button" class="lens-trail-loom-step${current ? " current" : ""}" data-lens-trail-step="${index}"${current ? ' aria-current="step"' : ""}>
          <small>${escapeHtml(String(subject.kind || "evidence").replaceAll("_", " "))}</small>
          <strong>${escapeHtml(subject.label || "Evidence")}</strong>
          <span>${summary.resolved}${summary.bounded ? "+" : ""} nearby route${summary.resolved === 1 ? "" : "s"}</span>
          ${mixes ? `<em>${mixes}</em>` : ""}${current ? "<u>You are here</u>" : ""}
        </button>`;
      }).join("");
      return `<nav class="lens-trail-loom" aria-label="How you got here">
        <header><span>How you got here</span><b>${trailHistory.length} step${trailHistory.length === 1 ? "" : "s"} · ${retained} route choice${retained === 1 ? "" : "s"} retained</b></header>
        <div class="lens-trail-loom-track">${steps}</div>
      </nav>`;
    }

    function renderLensTrail(data) {
      const root = traceResult.querySelector(".lens-trail");
      if (!root) return;
      currentTrail = data;
      const subject = data.subject || {};
      const facts = (data.facts || []).slice(0, 4).map((fact) => (
        `<span><b>${escapeHtml(fact.label)}</b>${escapeHtml(fact.value)}</span>`
      )).join("");
      const groups = new Map();
      (data.connections || []).forEach((connection, index) => {
        const section = connection.section || "knowledge";
        if (!groups.has(section)) groups.set(section, []);
        groups.get(section).push({ connection, index });
      });
      const connections = ["meaning", "artifacts", "knowledge", "data"].map((section) => {
        const items = groups.get(section) || [];
        if (!items.length) return "";
        return `<div class="lens-trail-group"><h4>${lensTrailSectionLabel(section)}</h4><section>${items.map(({ connection, index }) => {
          const context = lensTrailConnectionContext(connection.handle);
          return `<article><div><span>${escapeHtml(connection.relationship || "related to")}</span>`
            + `<strong>${escapeHtml(connection.label || "Related evidence")}</strong>`
            + `${connection.detail ? `<small>${escapeHtml(connection.detail)}</small>` : ""}`
            + `${context ? `<em class="lens-trail-route-context ${context.kind}">${escapeHtml(context.label)}</em>` : ""}</div>`
            + `<footer><button type="button" data-lens-trail-hop="${index}">${context?.kind === "return" ? "Return" : "Follow"}</button>`
            + `${connection.url ? `<a href="${escapeHtml(connection.url)}" target="_blank" rel="noopener">Open ↗</a>` : ""}</footer></article>`;
        }).join("")}</section></div>`;
      }).join("");
      const routeSummary = lensTrailRouteSummary(data);
      root.hidden = false;
      root.innerHTML = `${trailHistory.length > 1 ? '<button type="button" class="lens-trail-back">← Previous breadcrumb</button>' : ""}`
        + lensTrailLoomMarkup()
        + `<header><span>Current evidence · ${routeSummary.resolved}${routeSummary.bounded ? "+" : ""} nearby route${routeSummary.resolved === 1 ? "" : "s"}</span><strong>${escapeHtml(subject.label || "Selected evidence")}</strong>`
        + `${subject.detail ? `<p>${escapeHtml(subject.detail)}</p>` : ""}${facts ? `<div>${facts}</div>` : ""}</header>`
        + (connections || '<p class="lens-trail-empty">No further breadcrumbs surfaced. This object is still a valid endpoint.</p>');
      window.requestAnimationFrame(() => {
        traceView.scrollTop = traceView.scrollHeight;
      });
      window.requestAnimationFrame(constrainLens);
    }

    function showLensTrailStep(index) {
      if (index < 0 || index >= trailHistory.length) return;
      trailHistory = trailHistory.slice(0, index + 1);
      renderLensTrail(trailHistory[index].data);
    }

    function followLensTrailConnection(connection, button = null) {
      if (!connection?.handle) return;
      const earlier = lensTrailFrameIndex(connection.handle);
      if (earlier >= 0) {
        showLensTrailStep(earlier);
        return;
      }
      followTrail(connection.handle, button, { via: connection });
    }

    async function followTrail(handle, button = null, { push = true, via = null } = {}) {
      if (!handle) return;
      const root = traceResult.querySelector(".lens-trail");
      const request = ++trailRequest;
      if (button) {
        button.disabled = true;
        button.textContent = "Following…";
      }
      if (root) {
        root.hidden = false;
        root.innerHTML = '<div class="lens-trail-loading"><i></i><span>Connecting the breadcrumbs…</span></div>';
      }
      try {
        const data = await fetchJson("/api/calliope/trails", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ handle, limit: 24 }),
        });
        if (request !== trailRequest) return;
        const canonical = data.subject?.handle || handle;
        if (push) {
          const earlier = lensTrailFrameIndex(canonical);
          if (earlier >= 0) {
            trailHistory = trailHistory.slice(0, earlier + 1);
            trailHistory[earlier].data = data;
          } else {
            trailHistory.push({ handle: canonical, data, via });
          }
        } else if (trailHistory.length) {
          const frame = trailHistory.at(-1);
          frame.handle = canonical;
          frame.data = data;
        } else {
          trailHistory.push({ handle: canonical, data, via });
        }
        renderLensTrail(data);
        if (button) button.textContent = "Trail open";
      } catch (error) {
        if (request !== trailRequest) return;
        if (button) {
          button.disabled = false;
          button.textContent = "Follow trail";
        }
        if (root) root.innerHTML = `<p class="lens-trail-error">${escapeHtml(error instanceof Error ? error.message : String(error))}</p>`;
      }
    }

    trigger.addEventListener("click", (event) => {
      if (performance.now() < suppressTriggerClickUntil) {
        event.preventDefault();
        return;
      }
      setOpen(shell.dataset.open !== "true");
    });
    close.addEventListener("click", () => {
      setOpen(false);
      trigger.focus();
    });
    tabs.forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view, true)));
    queryBrowser.addEventListener("click", () => {
      openDashboardQuery(queryContext?.kind === "dashboard" ? queryContext.key : "");
    });
    querySource.addEventListener("change", () => openDashboardQuery(querySource.value));
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
      if (handoff) {
        askCalliope(handoff);
        return;
      }
      const trail = event.target.closest(".trail-follow");
      if (trail) {
        trailHistory = [];
        followTrail(currentTrailHandle(), trail);
        return;
      }
      if (event.target.closest(".watch-open")) {
        openWatchPanel();
        return;
      }
      if (event.target.closest(".watch-panel-close")) {
        watchPanelOpen = false;
        renderWatchPanel();
        return;
      }
      const watchCheck = event.target.closest("[data-watch-check]");
      if (watchCheck) {
        void changeSemanticWatch(watchCheck, "check");
        return;
      }
      const watchActive = event.target.closest("[data-watch-active]");
      if (watchActive) {
        void changeSemanticWatch(watchActive, "active");
        return;
      }
      const watchDelete = event.target.closest("[data-watch-delete]");
      if (watchDelete) {
        void changeSemanticWatch(watchDelete, "delete");
        return;
      }
      if (event.target.closest(".lens-trail-back")) {
        if (trailHistory.length > 1) {
          showLensTrailStep(trailHistory.length - 2);
        }
        return;
      }
      const trailStep = event.target.closest("[data-lens-trail-step]");
      if (trailStep) {
        showLensTrailStep(Number(trailStep.dataset.lensTrailStep));
        return;
      }
      const hop = event.target.closest("[data-lens-trail-hop]");
      if (hop && currentTrail) {
        const connection = (currentTrail.connections || [])[Number(hop.dataset.lensTrailHop)];
        followLensTrailConnection(connection, hop);
        return;
      }
      const homePin = event.target.closest(".home-pin");
      if (homePin) pinSemanticObjectToHome(homePin);
    });
    traceResult.addEventListener("submit", (event) => {
      const form = event.target.closest(".watch-form");
      if (!form) return;
      event.preventDefault();
      void createSemanticWatch(form);
    });
    queryDrawer.addEventListener("click", (event) => {
      if (event.target.closest(".query-sql-toggle")) {
        setQuerySqlVisible(querySqlToggle.getAttribute("aria-pressed") !== "true");
        return;
      }
      if (event.target.closest(".query-sql-copy")) {
        copyQuerySql();
        return;
      }
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
    [panelHeader, trigger].forEach((handle) => {
      handle.addEventListener("pointerdown", beginLensDrag);
      handle.addEventListener("pointermove", moveLens);
      handle.addEventListener("pointerup", endLensDrag);
      handle.addEventListener("pointercancel", endLensDrag);
    });
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
      if (pickerEnabled() && pickedElement) positionOutline(pickedElement, true);
      else outline.hidden = true;
      scheduleCandidateHighlights();
    }, { passive: true });
    window.addEventListener("resize", () => {
      if (pickerEnabled() && pickedElement) positionOutline(pickedElement, true);
      else outline.hidden = true;
      constrainLens();
      window.requestAnimationFrame(constrainLens);
      scheduleCandidateHighlights();
    });
    window.addEventListener("rvbbit:query-trace", () => {
      syncQueryBrowser();
      if (shell.dataset.view === "trace") {
        const count = traceEntries().length;
        pickStatus.title = count ? `${count} dashboard quer${count === 1 ? "y" : "ies"} traced` : "";
        scheduleCandidateHighlights();
      }
    });
    window.addEventListener("rvbbit:semantic-map-ready", () => {
      const count = semanticEntries().length;
      pickStatus.title = count
        ? `${count} named business object${count === 1 ? "" : "s"}`
        : pickStatus.title;
      scheduleCandidateHighlights();
    });
    window.addEventListener("rvbbit:semantic-object", scheduleCandidateHighlights);
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
    revealLensWhenReady = () => {
      if (!stylesheetReady || stylesheetFailed || !lensReady || !host.isConnected) return;
      host.style.removeProperty("visibility");
      host.removeAttribute("aria-hidden");
      initializePosition();
      window.requestAnimationFrame(initializePosition);
    };
    if (sessionGet(openKey) === "1") setOpen(true);
    syncQueryBrowser();
    pollSemanticEnrichment();
    restoreScroll();
    lensReady = true;
    revealLensWhenReady();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
})();
