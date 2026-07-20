# The Field Guide — onboarding, tutorial & demo plan

*Drafted 2026-07-20. The blank-slate cliff is the top post-launch UX gap;
this is the fix. Companion decision: AI providers become first-class
config (see §7) — the guide depends on knowing whether intelligence is
configured, but never requires it.*

## 1. Thesis

**No tour engine. Furniture, a field guide, and the assistant as tutor
when she's awake.**

Tooltip-crawl product tours are built for linear SaaS forms. DataRabbit
is a spatial, open-world desktop — overlay tours fight the metaphor, rot
with every UI change, and get closed by reflex. The product already owns
three better substrates:

1. **Furniture** — pre-arranged desktops. First run lands in a furnished
   room, not a void.
2. **Runnable artifacts** — built-not-run SQL, pre-filled blocks, plates,
   layouts. The house doctrine (hand the user the artifact, never run it
   for them) IS the pedagogy.
3. **The assistant** — she can open panels deep-linked, create sized and
   placed blocks, install plates, open layout walls, and screenshot her
   own work. A guide who performs the tour and takes questions beats any
   script. But she is the DELUXE tour, never the only tour — most fresh
   installs will not have an LLM configured (§7).

## 2. The degradation matrix (design to the worst row)

| Install | What works |
|---|---|
| Plain Postgres, no rvbbit | Core chapters (~6): loader, Finder, query+chart, click-to-filter, saved views/scenes, palette. All via Do-it buttons — no assistant, no plates. |
| rvbbit, no LLM configured | + acceleration, time travel, kits/plates/layouts, System Objects — all teachable via built-not-run artifacts. Semantic/assistant chapters show LOCKED with a "wake the assistant" pointer (§7 ladder). |
| rvbbit + intelligence | Everything, plus "Show me around" — the assistant performs the tour. |

Every chapter must complete WITHOUT the assistant. Her tour is the same
curriculum performed live, not separate content.

## 3. P0 — the Bigfoot loader (data first, one click)

Every demo needs rows; chapter zero is data.

- **Detection**: connection with zero user tables (excluding rvbbit/system
  schemas) → the Finder renders a hero instead of a void: *"Nothing here
  yet. Load the Bigfoot field data (~40k sightings, one click) — or
  import your own CSV."* Also exposed permanently in the Field Guide and
  the command palette ("Load sample data").
- **Loader**: lens route `/api/demo/load` running a bundled seed
  (schema + COPY-style inserts, idempotent, ~seconds). Plain PG: tables
  only. `hasRvbbit`: additionally accelerate the big table and install
  the existing Bigfoot demo kit (`demo/*` plates already ship). Returns
  progress; the Finder refreshes into a populated tree.
- The seed lives with the lens (works without the extension); the demo
  kit install reuses the existing kit path.

## 4. P1 — the Field Guide window

A lens-native checklist panel. Registry-driven, like `desktop_panels`:
one curriculum file, mode-filtered — **the stripped-down Postgres version
is a filter, not a fork.**

```ts
interface GuideChapter {
  id: string
  title: string
  blurb: string            // two sentences: what & why
  mode: "core" | "rvbbit"  // rvbbit chapters hidden on plain PG
  needs?: "data" | "intelligence"  // gates with a pointer, never hides
  action:                  // the Do-it button — launches, never lectures
    | { kind: "loader" }
    | { kind: "panel"; panel: string; hint?: string }   // open_panel path
    | { kind: "sql"; title: string; sql: string; run?: boolean } // built-not-run teaching query
    | { kind: "block"; title: string; sql: string; chart?: object } // pre-filled block
    | { kind: "layout"; layoutId: string }
    | { kind: "assistant"; seed: string }               // opens dock, seeds prompt
}
```

- Completion persisted per browser (homebase, like dock state); manual
  re-open from the System folder, ⌘P, and the Desktop menu ("Field
  Guide").
- `needs: "intelligence"` chapters render locked with the §7 ladder link
  — visible so users know what they're missing, never dead buttons.

### Chapter list v1

Core (both modes):
1. **Load the field data** — loader.
2. **Meet the Finder** — panel: finder; blurb teaches the vitals column.
3. **Query it, chart it** — pre-filled block: sightings by season, chart
   tab active.
4. **Click to filter** — two pre-wired blocks; click a bar, watch the
   detail follow (the param bus is the product's soul — show it early).
5. **Keep what you made** — save a view, desktop icon, save the desktop
   as a Scene.
6. **Move fast** — ⌘P palette, workspaces, the OS bar.

rvbbit:
7. **Wake the assistant** — needs: intelligence; the §7 ladder. Once
   awake: "ask her where anything lives" (the 0190 help system).
8. **Accelerate a table** — built-not-run: accelerate + the SAME query
   before/after with timings visible. The product's first wow.
9. **Time travel** — built-not-run AS-OF teaching query with comments.
10. **Ask what it means** — semantic ops on sighting descriptions
    (`means()` over spooky text is the perfect first semantic query);
    needs: intelligence.
11. **Kits & plates** — open the shelf, open a demo plate, view source
    (the source menu shows it's all rows).
12. **Layouts** — open the demo layout wall; stamp it; save-arrangement.
13. **Watch your data** — metrics + a KPI alert on sightings.
14. **The full map** — System Objects, Monitor, capability catalog; "or
    just ask the assistant where things live."

## 5. P2 — the assistant tour

- A "Show me around" chip in the Field Guide header (enabled when
  intelligence is configured) opens the dock with a seeded prompt.
- Curriculum discovery follows the 0190 pattern: the guide registry syncs
  to `rvbbit.field_guide_chapters` (or rides `desktop_panels.notes`);
  one prompt bullet teaches her to read it and PERFORM chapters on
  request — open the panel, build the block, narrate, ask "want the
  next one?" Zero per-turn context tax.
- She adapts: performs chapter 4 by building both blocks and telling the
  user to click; performs chapter 8 by running the timings herself and
  reporting the delta.

## 6. P3 — polish

- Completion states + a quiet "guide finished" moment.
- Maybe ONE micro-affordance: a chapter can pulse a `data-tour-target`
  element once (no overlay engine, no step sequences).
- First-run furnished room: on a fresh homebase, auto-open the Field
  Guide + Finder + a welcome note window instead of the void.

## 7. Companion decision — AI providers become first-class

The guide's biggest fork is "is intelligence configured?" — which today
is buried in capability installs. Decision (2026-07-20): **provider
credentials become first-class configuration; capabilities keep
deployable things.**

- The distinction that untangles it: a *capability* is something the
  SYSTEM can do (a deployed model, an MCP server, a kit). A *provider
  credential* is something the USER has. Conflating them is the
  inconsistency — nobody "installs a capability" to add a Postgres
  connection, and LLM keys are the same species as connections.
- The system is ALREADY first-class covertly (backends table, model
  settings, operator provider configs); this adds the missing honest
  front door, not a new system. The UI writes the same rows the
  capability path writes today.
- **The ladder** (one window, "AI Providers", Connections-grade UX):
  1. Managed Clover — free tier, no key, one click (the true
     out-of-box path).
  2. BYOK — paste an OpenRouter / Anthropic / OpenAI / Google key →
     fetch live model list → pick defaults (assistant model, semantic
     model, embeddings).
  3. OAuth where real (OpenRouter PKCE) — click, approve, key arrives.
  4. Local — detect Ollama/vLLM/warren on localhost, zero keys.
- Field Guide chapter 7 IS this ladder; `needs: "intelligence"` gates
  resolve against "any configured provider passing a test call."
- Scope guard: no backends.rs rewrite. Warren/HF deploys stay
  capabilities — they deploy things. This is credentials + model lists +
  defaults + a test button.

## 8. Non-goals

- Spotlight/coachmark overlay tours as a primary mechanism.
- Video embeds, external docs dependence for the core loop.
- A separate "tutorial mode" — the guide lives in the real desktop with
  real data, because that IS the pitch.

## 9. Open questions

- Loader dataset size/shape: full BFRO-style corpus vs a trimmed 10-40k
  rows (lean: trimmed, seconds-fast, text-rich for semantic chapters).
- Should completing core chapters on plain PG end with a tasteful "this
  desktop grows teeth with the rvbbit extension" chapter? (Lean: yes,
  one honest chapter, no nagging.)
- Guide window placement on small screens; whether the checklist rides
  the OS bar as a progress chip during first-run only.
- Providers window naming: "AI Providers" vs "Intelligence" vs
  "Models" (lean: AI Providers — boring and unmistakable).
