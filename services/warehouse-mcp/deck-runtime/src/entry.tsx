/* window.RvbbitDeck — render an rvbbit deck spec with the vendored
 * bolt-slides engine. See docs/DECKS_PLAN.md §3.
 *
 * Spec shape:
 *   { deck: { title, theme?: {tokens}, slides: [
 *       { component, props?, notes?, nav?,
 *         data?: { sql, bind, shape?: "rows"|"points"|"cell", mode?: "pinned"|"live", rows? } }
 *   ] } }
 *
 * Data binding:
 *   pinned → data.rows was embedded at publish time; bound synchronously.
 *   live   → window.rvbbitQuery(sql) (injected by the /d/ host) is called at
 *            mount; the slide renders with pinned rows (if any) until it lands.
 *   shape "rows"  → props[bind] = rows as-is (SQL aliases ARE the prop keys)
 *   shape "points"→ props[bind] = first numeric column as number[]
 *   shape "cell"  → props[bind] = first cell of first row
 */
import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import Deck from "./vendor/deck/Deck";
import { ErrorSlide, REGISTRY } from "./registry";
import "./vendor/styles/tokens.css";
import "./vendor/styles/base.css";

type Row = Record<string, unknown>;

interface SlideSpec {
  component: string;
  props?: Record<string, unknown>;
  notes?: string;
  nav?: string;
  data?: {
    sql?: string;
    bind: string;
    shape?: "rows" | "points" | "cell";
    /** Column to pluck for points/cell shapes; default = first numeric
     *  (points) / first column (cell). Aliases in the SQL are the API. */
    column?: string;
    mode?: "pinned" | "live";
    rows?: Row[];
  };
}

interface DeckSpec {
  deck: {
    title?: string;
    theme?: Record<string, string>;
    slides: SlideSpec[];
  };
}

declare global {
  interface Window {
    rvbbitQuery?: (sql: string) => Promise<{ rows?: Row[] } | Row[]>;
    RvbbitDeck?: { render: (el: HTMLElement, spec: DeckSpec) => void; version: string };
  }
}

function shapeRows(rows: Row[], shape: string, column?: string): unknown {
  if (shape === "cell") {
    const first = rows[0] ?? {};
    const k = column && column in first ? column : Object.keys(first)[0];
    return k ? first[k] : null;
  }
  if (shape === "points") {
    return rows.map((r) => {
      if (column && column in r) {
        const n = Number(r[column]);
        return Number.isFinite(n) ? n : 0;
      }
      for (const v of Object.values(r)) {
        const n = typeof v === "number" ? v : Number(v);
        if (Number.isFinite(n)) return n;
      }
      return 0;
    });
  }
  return rows;
}

function DataBound({ spec, children }: { spec: SlideSpec; children: (extra: Record<string, unknown>) => React.ReactNode }) {
  const d = spec.data;
  const pinned = d?.rows ? { [d.bind]: shapeRows(d.rows, d.shape ?? "rows", d.column) } : {};
  const [extra, setExtra] = useState<Record<string, unknown>>(pinned);
  useEffect(() => {
    if (!d || d.mode !== "live" || !d.sql || !window.rvbbitQuery) return;
    let dead = false;
    window
      .rvbbitQuery(d.sql)
      .then((res) => {
        const rows: Row[] = Array.isArray(res) ? res : (res?.rows ?? []);
        if (!dead && rows.length) setExtra({ [d.bind]: shapeRows(rows, d.shape ?? "rows", d.column) });
      })
      .catch(() => { /* keep pinned rows — live is best-effort */ });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <>{children(extra)}</>;
}

function SlideFromSpec({ s, i }: { s: SlideSpec; i: number }) {
  const C = REGISTRY[s.component];
  if (!C) return <>{ErrorSlide({ name: s.component, error: `unknown component (slide ${i + 1})` })}</>;
  const base = { ...(s.props ?? {}), nav: s.nav ?? (s.props?.nav as string | undefined), notes: s.notes };
  if (!s.data) return <C {...base} />;
  return <DataBound spec={s}>{(extra) => <C {...base} {...extra} />}</DataBound>;
}

function applyTheme(theme?: Record<string, string>) {
  if (!theme) return;
  const root = document.documentElement;
  for (const [k, v] of Object.entries(theme)) {
    root.style.setProperty(k.startsWith("--") ? k : `--${k}`, v);
  }
}

/* Sandboxed hosts (the lens renders decks in a srcdoc iframe, origin null)
 * reject history.replaceState/pushState with absolute URLs — the engine's
 * hash-sync would throw and kill the mount. Make history writes
 * best-effort everywhere; hash reads and in-deck navigation still work.
 * This lives in OUR layer — the vendored engine stays untouched. */
function shimHistoryForSandbox() {
  const wrap = <T extends typeof history.replaceState>(fn: T): T =>
    function (this: History, ...args: Parameters<T>) {
      try {
        return fn.apply(history, args)
      } catch {
        /* sandboxed frame — navigation state is in-memory only */
      }
    } as T
  try {
    history.replaceState = wrap(history.replaceState.bind(history))
    history.pushState = wrap(history.pushState.bind(history))
  } catch {
    /* history not writable — nothing to shim */
  }
}

/* Accent-follows-desktop: the lens materializes its live theme into deck
 * iframes as --main/--background/… (themeStyleTag). When --main is present,
 * derive the deck's accent family from it so any deck opened on the
 * desktop matches the CURRENT theme — including decks published before a
 * retheme. Precedence: spec theme applies first, derived tokens LAST —
 * on the desktop, the desktop wins (that's the point); on /apps and the
 * hub --main is absent, so the spec's authored accent governs. Authors
 * can pin their accent everywhere with theme {"follow-desktop":"off"}.
 * Conservative by design: only the accent family follows — the deck
 * keeps its own dark canvas, because mapping a light desktop background
 * onto slides built for dark would shred contrast. */
function deriveThemeFromDesktop(): Record<string, string> | null {
  const main = getComputedStyle(document.documentElement).getPropertyValue("--main").trim();
  if (!main) return null;
  // Resolve to rgb via the engine (handles oklch/hex/named) for luminance.
  let ink = "#04140e";
  try {
    const probe = document.createElement("div");
    probe.style.color = main;
    probe.style.display = "none";
    document.body.appendChild(probe);
    const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g)?.map(Number) ?? [];
    probe.remove();
    if (rgb.length >= 3) {
      const [r, g, b] = rgb;
      const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      ink = lum > 0.6 ? "#04140e" : "#f4f7fa";
    }
  } catch {
    /* keep default ink */
  }
  return {
    "--primary": main,
    "--accent": main,
    "--accent-ink": ink,
    "--bg-grad-1": `color-mix(in oklab, ${main} 14%, transparent)`,
    "--bg-grad-2": `color-mix(in oklab, ${main} 7%, transparent)`,
  };
}

function render(el: HTMLElement, spec: DeckSpec) {
  shimHistoryForSandbox()
  applyTheme(spec.deck.theme);
  if (spec.deck.theme?.["follow-desktop"] !== "off") {
    const derived = deriveThemeFromDesktop();
    if (derived) applyTheme(derived);
  }
  if (spec.deck.title) document.title = spec.deck.title;
  const slides = (spec.deck.slides ?? []).map((s, i) => <SlideFromSpec key={i} s={s} i={i} />);
  createRoot(el).render(<Deck>{slides}</Deck>);
}

window.RvbbitDeck = { render, version: "0.1.2" };
export { render };
