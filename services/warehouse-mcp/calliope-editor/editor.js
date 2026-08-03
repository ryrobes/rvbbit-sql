import { Compartment, EditorState } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  MatchDecorator,
  ViewPlugin,
  keymap,
  placeholder,
} from "@codemirror/view";
import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";

const OBJECT_KINDS = new Set(["person", "place", "thing", "project", "ticket"]);
const markerMatcher = new MatchDecorator({
  regexp: /\[\[(person|place|thing|project|ticket):\d+\|[^\]\r\n]+\]\]/gi,
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

function safeLabel(value) {
  return String(value || "Object")
    .replace(/[\]|]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 240) || "Object";
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

function objectCompletionSource(lookup) {
  return async (context) => {
    const token = context.matchBefore(/\[\[(?:(?:person|place|thing|project|ticket):)?[^\]\n]{0,100}$/i);
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
      options: (objects || []).map((object, index) => {
        const kind = OBJECT_KINDS.has(object.kind) ? object.kind : "thing";
        const label = safeLabel(object.label);
        return {
          label,
          displayLabel: label,
          detail: `${kind.toUpperCase()} · ${object.source || "Company knowledge"}`,
          type: "variable",
          boost: 100 - index,
          apply: `[[${kind}:${object.node_id}|${label}]]`,
        };
      }),
      validFor: /^\[\[(?:(?:person|place|thing|project|ticket):)?[^\]\n]{0,100}$/i,
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

function mount(host, options = {}) {
  if (!host) throw new Error("A Daily Notes editor host is required");
  const editable = new Compartment();
  const submit = () => {
    if (typeof options.onSubmit === "function") options.onSubmit();
    return true;
  };
  const view = new EditorView({
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
        placeholder(options.placeholder || "What should future-you and Calliope remember?"),
        keymap.of([
          { key: "Mod-Enter", run: submit },
          ...completionKeymap,
          ...historyKeymap,
          ...defaultKeymap,
        ]),
        autocompletion({
          override: [objectCompletionSource(options.lookup || (async () => []))],
          activateOnTyping: true,
          maxRenderedOptions: 20,
        }),
        markerDecorations,
        noteTheme,
        editable.of(EditorView.editable.of(true)),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && typeof options.onChange === "function") {
            options.onChange(update.state.doc.toString());
          }
        }),
      ],
    }),
  });
  return {
    getValue: () => view.state.doc.toString(),
    setValue(value = "") {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: String(value) } });
    },
    insertText(value = "") {
      const selection = view.state.selection.main;
      const documentText = view.state.doc.toString();
      const insert = speechInsertion(documentText, selection.from, selection.to, value);
      if (!insert) return false;
      const maxLength = Number(options.maxLength || 0);
      if (
        maxLength > 0
        && documentText.length - (selection.to - selection.from) + insert.length > maxLength
      ) return false;
      view.dispatch({
        changes: { from: selection.from, to: selection.to, insert },
        selection: { anchor: selection.from + insert.length },
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
    destroy: () => view.destroy(),
  };
}

window.CalliopeDailyNotesEditor = Object.freeze({ mount });
