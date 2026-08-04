import { Compartment, EditorState, StateEffect, StateField } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  MatchDecorator,
  ViewPlugin,
  keymap,
  placeholder,
} from "@codemirror/view";
import { autocompletion, completionKeymap, startCompletion } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";

const OBJECT_KINDS = new Set([
  "person", "place", "thing", "project", "ticket", "metric", "artifact",
  "workflow", "instrument", "action", "capability",
]);
const markerMatcher = new MatchDecorator({
  regexp: /\[\[([a-z][a-z0-9_-]{0,31}):[^\]|\r\n]+\|[^\]\r\n]+\]\]/gi,
  decoration: (match) => Decoration.mark({
    class: `cm-note-object cm-note-object-${match[1].toLowerCase()}`,
  }),
});

const markerDecorations = ViewPlugin.fromClass(class {
  constructor(view) {
    this.decorations = markerMatcher.createDeco(view);
  }

  update(update) {
    this.decorations = markerMatcher.updateDeco(update, this.decorations);
  }
}, { decorations: (value) => value.decorations });

const setObjectHintDecorations = StateEffect.define();
const setActiveObjectHint = StateEffect.define();
const objectHintDecorationField = StateField.define({
  create: () => Decoration.none,
  update(value, transaction) {
    let next = transaction.docChanged ? Decoration.none : value.map(transaction.changes);
    for (const effect of transaction.effects) {
      if (!effect.is(setObjectHintDecorations)) continue;
      next = Decoration.set((effect.value || []).map((hint) => Decoration.mark({
        class: `cm-object-hint${hint.objects.length > 1 ? " cm-object-hint-ambiguous" : ""}`,
        attributes: {
          "data-object-hint-key": hint.key,
          title: hint.objects.length > 1
            ? `${hint.objects.length} company references match · click to choose`
            : `${hint.objects[0].kind}: ${hint.objects[0].label} · click to tag`,
        },
      }).range(hint.from, hint.to)), true);
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field),
});
const activeObjectHintField = StateField.define({
  create: () => null,
  update(value, transaction) {
    let next = transaction.docChanged || transaction.selection ? null : value;
    for (const effect of transaction.effects) {
      if (effect.is(setActiveObjectHint)) next = effect.value;
    }
    return next;
  },
});

const HINT_CUE_KINDS = new Map([
  ["person", "person"], ["people", "person"],
  ["place", "place"], ["location", "place"],
  ["project", "project"],
  ["ticket", "ticket"], ["issue", "ticket"],
  ["metric", "metric"],
  ["dashboard", "artifact"], ["app", "artifact"], ["artifact", "artifact"],
  ["workflow", "workflow"], ["instrument", "instrument"],
  ["action", "action"], ["capability", "capability"],
]);
const HINT_LEADING_COMMANDS = new Set([
  "add", "analyze", "ask", "build", "check", "compare", "consider", "create",
  "explain", "explore", "find", "investigate", "look", "open", "review",
  "search", "show", "tell", "update", "use",
]);
const HINT_COMMON_WORDS = new Set([
  "about", "after", "again", "also", "another", "around", "because", "before",
  "being", "best", "better", "between", "build", "calliope", "capability",
  "could", "dashboard", "data", "does", "doing", "done", "each", "either",
  "else", "every", "find", "from", "give", "have", "help", "here", "instrument",
  "into", "issue", "just", "like", "look", "make", "maybe", "metric", "might",
  "more", "most", "need", "only", "other", "person", "place", "please", "project",
  "should", "show", "some", "someone", "something", "somewhere", "table", "tables",
  "tell", "than", "that", "their", "them", "then", "there", "these", "thing",
  "things", "think", "this", "those", "through", "ticket", "want", "what",
  "whatever", "when", "where", "which", "while", "with", "workflow", "would",
  "your",
]);

function objectHintCandidates(value) {
  const text = String(value || "");
  if (text.length < 2) return [];
  const blocked = [];
  const block = (regexp) => {
    for (const match of text.matchAll(regexp)) {
      blocked.push([match.index, match.index + match[0].length]);
    }
  };
  block(/\[\[[a-z][a-z0-9_-]{0,31}:[^\]|\r\n]{1,240}\|[^\]\r\n]{1,240}\]\]/gi);
  block(/https?:\/\/[^\s<>{}\[\]]+/gi);
  block(/```[\s\S]*?```|`[^`\r\n]+`/g);
  const overlapsBlocked = (from, to) => blocked.some(([start, end]) => from < end && to > start);
  const byRange = new Map();
  const add = (from, to, priority, kind = "") => {
    let start = Math.max(0, from);
    let end = Math.min(text.length, to);
    while (start < end && /[\s"'“”‘’([{]/u.test(text[start])) start += 1;
    while (end > start && /[\s"'“”‘’.,;:!?\])}]/u.test(text[end - 1])) end -= 1;
    const candidateText = text.slice(start, end);
    if (
      candidateText.length < 2
      || candidateText.length > 100
      || /[\r\n]/.test(candidateText)
      || overlapsBlocked(start, end)
    ) return;
    const rangeKey = `${start}:${end}`;
    const existing = byRange.get(rangeKey);
    if (!existing || priority > existing.priority || (kind && !existing.kind)) {
      byRange.set(rangeKey, { from: start, to: end, text: candidateText, priority, kind });
    }
  };

  for (const match of text.matchAll(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi)) {
    add(match.index, match.index + match[0].length, 100, "person");
  }
  for (const match of text.matchAll(/\b[A-Z][A-Z0-9]{1,11}-\d+\b/g)) {
    add(match.index, match.index + match[0].length, 98, "ticket");
  }
  for (const match of text.matchAll(/\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b/g)) {
    add(match.index, match.index + match[0].length, 94);
  }
  for (const match of text.matchAll(/["“]([^"”\r\n]{2,100})["”]/g)) {
    const innerOffset = match[0].indexOf(match[1]);
    add(match.index + innerOffset, match.index + innerOffset + match[1].length, 92);
  }

  // Lowercase company terms are common in natural chat. Propose a bounded set
  // of meaningful tokens and short adjacent phrases, then let the ACL-aware
  // server keep only terms present in visible labels/ids. This is intentionally
  // not the combinatorial all-ngram path.
  const contentTokens = [];
  for (const match of text.matchAll(/\b_?[\p{L}][\p{L}\p{N}_.-]{3,}\b/gu)) {
    const token = match[0];
    const normalized = token.replace(/^_+/, "").toLocaleLowerCase();
    const technical = /[_.-]/.test(token);
    if (!technical && HINT_COMMON_WORDS.has(normalized)) continue;
    const candidate = {
      from: match.index,
      to: match.index + token.length,
      text: token,
      technical,
    };
    contentTokens.push(candidate);
    add(
      candidate.from,
      candidate.to,
      technical ? 84 : 62 + Math.min(token.length, 12),
    );
  }
  for (let index = 0; index < contentTokens.length; index += 1) {
    for (let width = 2; width <= 3; width += 1) {
      const group = contentTokens.slice(index, index + width);
      if (group.length !== width) continue;
      const adjacent = group.slice(1).every((token, offset) => (
        /^\s+$/.test(text.slice(group[offset].to, token.from))
      ));
      if (!adjacent) continue;
      add(group[0].from, group[group.length - 1].to, 74 + width);
    }
  }

  const cuePattern = /\b(person|people|place|location|project|ticket|issue|metric|dashboard|app|artifact|workflow|instrument|action|capability)\s+(?:(?:named|called)\s+)?/giu;
  for (const match of text.matchAll(cuePattern)) {
    const start = match.index + match[0].length;
    const tail = text.slice(start).match(/^["“]?([\p{L}\p{N}@][\p{L}\p{N}@._'’&\/-]*(?:\s+[\p{L}\p{N}@][\p{L}\p{N}@._'’&\/-]*){0,3})/u);
    if (!tail) continue;
    const phrase = tail[1];
    const phraseStart = start + tail[0].indexOf(phrase);
    const words = [...phrase.matchAll(/[\p{L}\p{N}@][\p{L}\p{N}@._'’&\/-]*/gu)];
    const kind = HINT_CUE_KINDS.get(match[1].toLowerCase()) || "";
    for (const word of words) {
      add(phraseStart, phraseStart + word.index + word[0].length, 88 + words.length, kind);
    }
  }

  const titlePattern = /\b(?:[\p{Lu}][\p{L}\p{N}'’._-]*|[A-Z]{2,})(?:\s+(?:(?:of|the|and|for|to|&)\s+)?(?:[\p{Lu}][\p{L}\p{N}'’._-]*|[A-Z]{2,})){0,4}/gu;
  for (const match of text.matchAll(titlePattern)) {
    const significant = [...match[0].matchAll(/[\p{Lu}][\p{L}\p{N}'’._-]*/gu)];
    const structuredSingle = significant.length === 1 && (/\d/.test(match[0]) || /^[A-Z]{2,}$/.test(match[0]));
    const promptedSingle = significant.length === 1 && /\b(?:ask|tell|with|about|for|from|to|on|owner|assignee|contact)\s+$/i.test(
      text.slice(Math.max(0, match.index - 24), match.index),
    );
    if (significant.length >= 2 || structuredSingle || promptedSingle) {
      add(match.index, match.index + match[0].length, 86 + Math.min(significant.length, 5));
      for (const divider of match[0].matchAll(/\s+and\s+/gi)) {
        add(match.index, match.index + divider.index, 85);
        add(
          match.index + divider.index + divider[0].length,
          match.index + match[0].length,
          85,
        );
      }
      // A capitalized command at the start of a sentence ("Ask Ada Lovelace")
      // should not swallow the proper name that follows it.
      if (
        significant.length >= 2
        && HINT_LEADING_COMMANDS.has(significant[0][0].toLowerCase())
      ) {
        add(
          match.index + significant[1].index,
          match.index + match[0].length,
          85 + Math.min(significant.length - 1, 5),
        );
      }
    }
  }

  return [...byRange.values()]
    .sort((left, right) => (
      right.priority - left.priority
      || (right.to - right.from) - (left.to - left.from)
      || left.from - right.from
    ))
    .slice(0, 12)
    .map((candidate) => ({
      ...candidate,
      key: `${candidate.from}-${candidate.to}`,
    }));
}

function resolvedObjectHints(response, candidates, documentText) {
  const byKey = new Map(candidates.map((candidate) => [candidate.key, candidate]));
  const rawHints = Array.isArray(response) ? response : response?.hints;
  const available = (Array.isArray(rawHints) ? rawHints : []).map((raw) => {
    const candidate = byKey.get(String(raw?.key || ""));
    const objects = Array.isArray(raw?.objects) ? raw.objects.filter((object) => (
      object && OBJECT_KINDS.has(object.kind) && safeRefId(object.ref_id ?? object.node_id)
    )) : [];
    if (
      !candidate
      || !objects.length
      || documentText.slice(candidate.from, candidate.to) !== candidate.text
    ) return null;
    return { ...candidate, objects };
  }).filter(Boolean).sort((left, right) => (
    (right.to - right.from) - (left.to - left.from)
    || right.priority - left.priority
    || left.from - right.from
  ));
  const selected = [];
  for (const hint of available) {
    if (selected.some((other) => hint.from < other.to && hint.to > other.from)) continue;
    selected.push(hint);
  }
  return selected.sort((left, right) => left.from - right.from);
}

function safeLabel(value) {
  return String(value || "Object")
    .replace(/[\]|]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 240) || "Object";
}

function safeRefId(value) {
  return String(value || "")
    .replace(/[\]|\r\n\x00-\x1f]/g, "")
    .trim()
    .slice(0, 240);
}

function parseObjectMarkers(value) {
  const text = String(value || "");
  const pattern = /\[\[([a-z][a-z0-9_-]{0,31}):([^\]|\r\n]{1,240})\|([^\]\r\n]{1,240})\]\]/gi;
  const found = [];
  const seen = new Set();
  for (const match of text.matchAll(pattern)) {
    const kind = match[1].toLowerCase();
    const refId = safeRefId(match[2]);
    if (!OBJECT_KINDS.has(kind) || !refId) continue;
    const key = `${kind}:${refId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    found.push({ kind, ref_id: refId, label: safeLabel(match[3]) });
  }
  return found;
}

function plainText(value) {
  return String(value || "").replace(
    /\[\[([a-z][a-z0-9_-]{0,31}):([^\]|\r\n]{1,240})\|([^\]\r\n]{1,240})\]\]/gi,
    (_match, _kind, _refId, label) => label,
  );
}

function speechInsertion(documentText, from, to, value) {
  const text = String(value || "").replace(/\r\n?/g, "\n").trim();
  if (!text) return "";
  const before = documentText.slice(0, from);
  const after = documentText.slice(to);
  const prefix = before && !/\s$/.test(before) && !/^[,.;:!?)}\]]/.test(text) ? " " : "";
  const suffix = after && !/^\s/.test(after) && !/[\s([{]$/.test(text) ? " " : "";
  return `${prefix}${text}${suffix}`;
}

function completionForObject(object, index, objectIndex) {
  const kind = OBJECT_KINDS.has(object.kind) ? object.kind : "thing";
  const label = safeLabel(object.label);
  const refId = safeRefId(object.ref_id ?? object.node_id);
  if (!refId) return null;
  objectIndex.set(`${kind}:${refId}`, {
    ...object,
    kind,
    ref_id: refId,
    label,
  });
  const summary = String(object.summary || "").replace(/\s+/g, " ").trim().slice(0, 90);
  return {
    label,
    displayLabel: label,
    detail: [kind.toUpperCase(), object.source || "Company knowledge", summary]
      .filter(Boolean)
      .join(" · "),
    type: "variable",
    boost: 100 - index,
    apply: `[[${kind}:${refId}|${label}]]`,
  };
}

function objectCompletionSource(lookup, objectIndex) {
  return async (context) => {
    const activeHint = context.state.field(activeObjectHintField, false);
    if (context.explicit && activeHint) {
      return {
        from: activeHint.from,
        to: activeHint.to,
        filter: false,
        options: activeHint.objects
          .map((object, index) => completionForObject(object, index, objectIndex))
          .filter(Boolean),
      };
    }
    const kinds = [...OBJECT_KINDS].join("|");
    const token = context.matchBefore(new RegExp(`\\[\\[(?:(?:${kinds}):)?[^\\]\\n]{0,100}$`, "i"));
    if (!token) return null;
    let fragment = token.text.slice(2);
    let requestedKind = "";
    const separator = fragment.indexOf(":");
    if (separator >= 0) {
      const candidate = fragment.slice(0, separator).toLowerCase();
      if (OBJECT_KINDS.has(candidate)) {
        requestedKind = candidate;
        fragment = fragment.slice(separator + 1);
      }
    }
    const query = fragment.replace(/\|.*$/, "").trim();
    if (query.length < 2) return null;
    let objects = [];
    try {
      objects = await lookup(query, requestedKind);
    } catch {
      return null;
    }
    return {
      from: token.from,
      to: context.pos,
      filter: false,
      options: (objects || [])
        .map((object, index) => completionForObject(object, index, objectIndex))
        .filter(Boolean),
      validFor: new RegExp(`^\\[\\[(?:(?:${kinds}):)?[^\\]\\n]{0,100}$`, "i"),
    };
  };
}

const noteTheme = EditorView.theme({
  "&": {
    minHeight: "108px",
    background: "transparent",
    color: "var(--bone)",
    font: "11px/1.58 var(--sans)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { overflow: "auto", fontFamily: "var(--sans)" },
  ".cm-content": { minHeight: "108px", padding: "11px 12px", caretColor: "var(--amber)" },
  ".cm-line": { padding: "0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--amber)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    background: "color-mix(in oklch,var(--amber) 24%,transparent)",
  },
  ".cm-placeholder": { color: "var(--dim)", fontStyle: "italic" },
  ".cm-gutters": { display: "none" },
  ".cm-note-object": {
    borderRadius: "3px",
    padding: "1px 2px",
    background: "color-mix(in oklch,var(--jade) 12%,transparent)",
    color: "var(--jade)",
    textDecoration: "underline",
    textDecorationColor: "color-mix(in oklch,var(--jade) 38%,transparent)",
    textUnderlineOffset: "3px",
  },
  ".cm-note-object-person": { color: "var(--amber)" },
  ".cm-tooltip": {
    overflow: "hidden",
    border: "1px solid var(--line-hot)",
    borderRadius: "5px",
    background: "var(--panel-raised)",
    color: "var(--bone)",
    boxShadow: "0 14px 36px rgba(0,0,0,.42)",
  },
  ".cm-tooltip-autocomplete > ul": { maxHeight: "240px", font: "9px/1.35 var(--mono)" },
  ".cm-tooltip-autocomplete > ul > li": { padding: "7px 9px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    background: "color-mix(in oklch,var(--amber) 13%,var(--panel-raised))",
    color: "var(--bone-bright)",
  },
  ".cm-completionDetail": { marginLeft: "10px", color: "var(--dim)", fontSize: "7px" },
});

const composerTheme = EditorView.theme({
  "&": {
    minHeight: "45px",
    maxHeight: "180px",
    background: "transparent",
    color: "var(--bone)",
    font: "12px/1.5 var(--sans)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { overflow: "auto", fontFamily: "var(--sans)" },
  ".cm-content": { minHeight: "45px", padding: "11px 11px 8px", caretColor: "var(--amber)" },
  ".cm-line": { padding: "0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--amber)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    background: "color-mix(in oklch,var(--amber) 24%,transparent)",
  },
  ".cm-placeholder": { color: "var(--dim)" },
  ".cm-gutters": { display: "none" },
  ".cm-note-object": {
    borderRadius: "999px",
    padding: "1px 4px",
    background: "color-mix(in oklch,var(--jade) 11%,transparent)",
    color: "var(--jade)",
    boxShadow: "inset 0 0 0 1px color-mix(in oklch,var(--jade) 28%,transparent)",
  },
  ".cm-note-object-person, .cm-note-object-project, .cm-note-object-ticket": {
    background: "color-mix(in oklch,var(--amber) 8%,transparent)",
    color: "var(--amber)",
    boxShadow: "inset 0 0 0 1px color-mix(in oklch,var(--amber) 26%,transparent)",
  },
  ".cm-object-hint": {
    borderRadius: "2px",
    cursor: "pointer",
    textDecorationLine: "underline",
    textDecorationStyle: "dotted",
    textDecorationThickness: "1px",
    textDecorationColor: "color-mix(in oklch,var(--jade) 64%,transparent)",
    textUnderlineOffset: "4px",
    transition: "background-color .14s ease,text-decoration-color .14s ease",
  },
  ".cm-object-hint:hover": {
    background: "color-mix(in oklch,var(--jade) 8%,transparent)",
    textDecorationColor: "var(--jade)",
  },
  ".cm-object-hint-ambiguous": {
    textDecorationColor: "color-mix(in oklch,var(--amber) 68%,transparent)",
  },
  ".cm-tooltip": {
    overflow: "hidden",
    border: "1px solid var(--line-hot)",
    borderRadius: "5px",
    background: "var(--panel-raised)",
    color: "var(--bone)",
    boxShadow: "0 14px 36px rgba(0,0,0,.42)",
  },
  ".cm-tooltip-autocomplete > ul": { maxHeight: "260px", font: "9px/1.35 var(--mono)" },
  ".cm-tooltip-autocomplete > ul > li": { padding: "7px 9px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    background: "color-mix(in oklch,var(--amber) 13%,var(--panel-raised))",
    color: "var(--bone-bright)",
  },
  ".cm-completionDetail": { marginLeft: "10px", color: "var(--dim)", fontSize: "7px" },
});

function mount(host, options = {}) {
  if (!host) throw new Error("A Calliope object editor host is required");
  const variant = options.variant === "composer" ? "composer" : "note";
  host.dataset.editorVariant = variant;
  const objectIndex = new Map();
  const objectHintIndex = new Map();
  const editable = new Compartment();
  const placeholderText = new Compartment();
  let hintTimer = null;
  let hintController = null;
  let hintGeneration = 0;
  let destroyed = false;
  let view;
  const scheduleObjectHints = (documentText) => {
    hintGeneration += 1;
    const generation = hintGeneration;
    window.clearTimeout(hintTimer);
    hintController?.abort();
    hintController = null;
    objectHintIndex.clear();
    if (variant !== "composer" || typeof options.hints !== "function") return;
    const candidates = objectHintCandidates(documentText);
    if (!candidates.length) return;
    hintTimer = window.setTimeout(async () => {
      const controller = new AbortController();
      hintController = controller;
      try {
        const response = await options.hints(candidates.map((candidate) => ({
          key: candidate.key,
          text: candidate.text,
          ...(candidate.kind ? { kind: candidate.kind } : {}),
        })), controller.signal);
        if (
          destroyed
          || controller.signal.aborted
          || generation !== hintGeneration
          || view.state.doc.toString() !== documentText
        ) return;
        const hints = resolvedObjectHints(response, candidates, documentText);
        objectHintIndex.clear();
        for (const hint of hints) objectHintIndex.set(hint.key, hint);
        view.dispatch({ effects: setObjectHintDecorations.of(hints) });
      } catch (error) {
        if (error?.name !== "AbortError") objectHintIndex.clear();
      } finally {
        if (hintController === controller) hintController = null;
      }
    }, 560);
  };
  const submit = () => {
    if (typeof options.onSubmit === "function") options.onSubmit();
    return true;
  };
  const submitKeys = variant === "composer"
    ? [{ key: "Enter", run: submit }]
    : [{ key: "Mod-Enter", run: submit }];
  view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: String(options.value || ""),
      extensions: [
        history(),
        EditorView.lineWrapping,
        EditorState.tabSize.of(2),
        EditorView.contentAttributes.of({
          "aria-label": options.ariaLabel || "Append a private note to this Daily Brief",
          "aria-multiline": "true",
        }),
        placeholderText.of(placeholder(options.placeholder || "What should future-you and Calliope remember?")),
        keymap.of([
          ...completionKeymap,
          ...submitKeys,
          ...historyKeymap,
          ...defaultKeymap,
        ]),
        autocompletion({
          override: [objectCompletionSource(options.lookup || (async () => []), objectIndex)],
          activateOnTyping: true,
          maxRenderedOptions: 20,
        }),
        markerDecorations,
        objectHintDecorationField,
        activeObjectHintField,
        variant === "composer" ? composerTheme : noteTheme,
        editable.of(EditorView.editable.of(true)),
        EditorView.domEventHandlers({
          paste(event) {
            return typeof options.onPaste === "function"
              ? options.onPaste(event) === true
              : false;
          },
          mousedown(event, currentView) {
            const target = event.target instanceof Element
              ? event.target.closest(".cm-object-hint")
              : null;
            const hint = target
              ? objectHintIndex.get(target.getAttribute("data-object-hint-key"))
              : null;
            if (!hint) return false;
            event.preventDefault();
            currentView.dispatch({
              selection: { anchor: hint.to },
              effects: setActiveObjectHint.of(hint),
              scrollIntoView: true,
            });
            currentView.focus();
            startCompletion(currentView);
            return true;
          },
        }),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            const documentText = update.state.doc.toString();
            scheduleObjectHints(documentText);
            if (typeof options.onChange === "function") options.onChange(documentText);
          }
        }),
      ],
    }),
  });
  scheduleObjectHints(view.state.doc.toString());
  return {
    getValue: () => view.state.doc.toString(),
    getPlainText: () => plainText(view.state.doc.toString()),
    getObjectRefs: () => parseObjectMarkers(view.state.doc.toString()).map((marker) => ({
      ...(objectIndex.get(`${marker.kind}:${marker.ref_id}`) || {}),
      ...marker,
    })),
    getSelection: () => ({
      from: view.state.selection.main.from,
      to: view.state.selection.main.to,
    }),
    setValue(value = "") {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: String(value) } });
    },
    setPlaceholder(value = "") {
      view.dispatch({ effects: placeholderText.reconfigure(placeholder(String(value))) });
    },
    insertText(value = "", requestedSelection = null) {
      const documentText = view.state.doc.toString();
      const requestedFrom = Number(requestedSelection?.from);
      const requestedTo = Number(requestedSelection?.to);
      const liveSelection = view.state.selection.main;
      const from = Number.isInteger(requestedFrom)
        ? Math.max(0, Math.min(requestedFrom, documentText.length))
        : liveSelection.from;
      const to = Number.isInteger(requestedTo)
        ? Math.max(from, Math.min(requestedTo, documentText.length))
        : liveSelection.to;
      const insert = speechInsertion(documentText, from, to, value);
      if (!insert) return false;
      const maxLength = Number(options.maxLength || 0);
      if (
        maxLength > 0
        && documentText.length - (to - from) + insert.length > maxLength
      ) return false;
      view.dispatch({
        changes: { from, to, insert },
        selection: { anchor: from + insert.length },
        userEvent: "input",
      });
      view.focus();
      return true;
    },
    setDisabled(disabled) {
      view.dispatch({ effects: editable.reconfigure(EditorView.editable.of(!disabled)) });
      host.classList.toggle("is-disabled", Boolean(disabled));
    },
    focus: () => view.focus(),
    destroy() {
      destroyed = true;
      window.clearTimeout(hintTimer);
      hintController?.abort();
      view.destroy();
    },
  };
}

const api = Object.freeze({ mount, parseObjectMarkers, plainText });
window.CalliopeObjectEditor = api;
window.CalliopeDailyNotesEditor = api;
