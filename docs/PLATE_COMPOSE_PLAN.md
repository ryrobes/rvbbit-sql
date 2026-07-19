# The Plate Compose Layer — Layouts

*Drafted 2026-07-19 from the layout conversations in KIT_PLATES_PLAN §30–§32.
Ryan settled the name: plainly, **layouts**.*

**STATUS (2026-07-19): P0–P2 BUILT AND BROWSER-VERIFIED** — 0187 schema +
upsert/patch/restore + revisions; wall mode (chromeless panes, fraction
translation, z, ESC ladder modal→zoom→wall, hover pill w/ zoom + pop-out,
wall-local modals, slot targeting via `@pane`); stamp mode; save-arrangement
round trip; shelf front-door rows; assistant `upsert_layout`/`patch_layout`/
`open_layout` + 0188 teaching. `crm/home` is the first live layout
(`kits.default_layout='crm/home'`). Remaining: P3 (min_width auto-pick,
phone variants), P4 polish (capture target `layout:x`, per-wall bus if
earned, role-gated panes). As-built deltas from the plan: modals are
WALL-LOCAL windows (a desktop window can't stack above the fixed wall
overlay — same stacking-tree reality as the markup editor); pane params
merge UNDER bus/local values (a pin is a default, never a lock); the
`upsert_layout` validator rejects non-arrangement pane keys at install.

## 1. Thesis

Plates gave kits durable surfaces; the compose layer gives kits **front
doors and arrangements**. A layout is a named, kit-shipped composition of
existing plates on a free-floating canvas — full-screen "wall" mode with
the desktop still behind it, or stamped onto the desktop as ordinary
windows. It answers "I click the CRM icon — what do I see?" without
inventing an app framework.

The desktop stays the native habitat (and the authoring surface). Layouts
are the optional arranged experience for people who want one.

## 2. Doctrine (the HyperCard wall)

**A layout owns arrangement, never behavior.**

- No buttons, no actions, no expressions, no event handlers on the layout
  row. Everything interactive lives on a plate, where interaction is
  already owned, sanitized, and actions-walled.
- A header bar with buttons is a **plate** (a toolbar plate — the
  switchboards already prove the pattern).
- The layout's entire orchestration budget: geometry, z-order, pinned
  params per pane, slot targets. Nothing else, ever.
- The orchestrator is the **param bus we already have**: panes host the
  same PlateWindow, so rv-emit → bus → from_bus cross-pane filtering works
  with zero new machinery. Click a customer in one pane, the inspector
  pane follows.

HyperCard died of script-soup: behavior attached to containers. The
layout is a picture frame with named holes; everything that thinks lives
in the plates.

## 3. Geometry: free-floating, not grid

Panes carry absolute rects + z-index on a canvas — Tableau-floating, not
tiled splits. Decided over grid-areas because:

1. **Desktop isomorphism.** A floating pane's data is exactly a desktop
   window rect minus chrome. Stamp mode (layout → windows) and the editor
   story (windows → layout) are the same transform run in both
   directions. A grid engine would need its own editor; floating gets a
   full WYSIWYG editor for free — the desktop itself.
2. **Compositional ceiling.** Layering (a full-bleed banner plate behind
   floating cards), overlaps, asymmetry — the stuff that makes Tableau
   floating dashboards nicer than tiled ones.
3. **No split-tree machinery.** Just rects. z is a plain integer.

### Responsiveness (no free lunch, so pick the honest lunch)

- Rects are stored as **fractions of a declared design size**
  (`design: {width, height}` on the row; pane x/y/w/h in 0..1).
- Render translates per-axis to the live viewport. **Content is never
  transform-scaled**: text stays 1:1 readable, plate HTML reflows, charts
  re-measure crisply via the §31 island-owned ResizeObserver.
- Per-axis scaling is monotone → panes that don't overlap at design size
  cannot start overlapping at render size. Layering stays intentional.
- Aspect drift is accepted. The real answer to a drastically different
  screen is a **sibling layout**: `crm/home` and `crm/home-phone` are
  separate rows with different geometry and possibly different pane sets
  (the phone wants 2 panes, not 6 squished ones). Layouts are cheap rows
  the assistant authors — a phone variant is one prompt.
- Optional `min_width` hint per layout; the launcher auto-picks the best
  variant for the viewport. Tableau's device designer, except every
  variant is a first-class artifact.

## 4. Schema sketch

```sql
CREATE TABLE rvbbit.plate_layouts (
    layout_id   text PRIMARY KEY,          -- 'crm/home'
    kit         text,
    title       text NOT NULL,
    description text,
    requires_role text,
    design      jsonb NOT NULL,            -- {"width":1600,"height":900,"min_width":1100}
    panes       jsonb NOT NULL,            -- see below
    created_at  timestamptz, updated_at timestamptz
);
-- pane: {"id":"inspector", "plate":"crm/customer-card",
--        "x":0.62,"y":0.08,"w":0.36,"h":0.55,"z":2,
--        "params":{"tab":"activity"},      -- pinned params, merged under bus
--        "slot":true}                      -- empty until targeted (see §6)
```

- `rvbbit.kits.default_layout` (nullable) → the front door. The kit icon
  opens it; the shelf lists the rest of the kit's layouts alongside its
  plates. Many layouts per kit is the *point* (dispatch wall / owner
  dashboard / front desk, possibly role-gated).
- Revisions: same trigger pattern as 0182 (`plate_layout_revisions`).
- `upsert_layout(...)` validates: every pane's plate exists (or is
  declared slot-only), fractions in range, ids unique. `validate_kit`
  checks layout→plate references. `export_kit` carries layouts.
- Assistant: `upsert_layout` / `patch_layout` commands (patch merges
  panes per id, null removes — same shape as patch_plate).

## 5. Rendering: wall mode and stamp mode

One row, two consumers:

- **Wall**: a full-screen overlay (desktop behind, per the render-parity
  principle). Panes are absolutely-positioned **chromeless** PlateWindows —
  no border, no title bar, no strip; the plate body only. ESC drops back
  to the desktop. tmux-isms: zoom-a-pane (temporarily maximize one),
  number-key focus. A quiet per-pane hover pill (plate title · pop out as
  window · re-render) is the debugging escape hatch; the strip's
  utilities (source menu, ms) live behind it.
- **Stamp**: materialize the panes as ordinary desktop windows arranged
  per the geometry — the layout as desktop arranger, for people who like
  the desktop (Scenes' cousin, but kit-shipped, connection-portable, and
  referencing plates instead of browser state).

Render refusals (contract gates, role walls) surface inline in the pane
body exactly as they do in windows — a gated pane shows its refusal, the
wall does not fail whole.

## 6. Transient plates: slots and windows, never dead panes

Edit/create plates are **not panes** — nothing renders stateless waiting
to be needed.

1. **Windows stay the modal layer.** `rv-open="plate:crm/edit"` from
   inside a pane opens a normal desktop window centered above the dimmed
   wall. Modals are just windows; the window manager, placement cascade,
   and edit-loop gesture already exist. No new noun.
2. **Slot panes** for master-detail: `"slot": true` renders a quiet empty
   state ("select a customer") and runs **zero queries** until targeted:
   `rv-open="plate:crm/customer-card@inspector"` renders into the named
   pane instead of spawning a window. One-token vocabulary extension.
   A slot with a `plate` value uses it as the default occupant; without
   one it starts empty.

## 7. The editor story

- **The assistant authors layouts** (rows about existing things — her
  best register), same opt-in doctrine as plates.
- **The desktop is the WYSIWYG editor**: open a layout in stamp mode (or
  start from scratch), drag/resize windows, then "Save arrangement as
  layout" normalizes the rects against the viewport and writes the row.
  Round trip: arrange in the medium you love, ship it as a composed
  artifact. No bespoke arranger UI in any phase.

## 8. Param bus semantics

Global bus, v1 — panes participate exactly as windows do, and
cross-surface filtering is a feature until proven otherwise. If two open
walls ever genuinely collide (two customer_id contexts), add per-wall bus
namespacing then, not before.

## 9. Phasing

- **P0** — schema + upsert/patch + wall mode read-only render (chromeless
  panes, fraction translation, z, ESC). Kit icon → default_layout.
- **P1** — slots + `@pane` targeting + windows-over-dimmed-wall + zoom.
- **P2** — stamp mode + save-arrangement-as-layout (the editor round trip).
- **P3** — sibling-layout auto-pick by viewport width; phone variants.
- **P4** — polish: hover pill, role-gated panes, per-wall bus if earned,
  assistant teachings (upsert_layout/patch_layout prompt migration).

## 10. Open questions

- The name. (the Pass / Spread / Deck / Screen.)
- Does a *pane* pin `requires_role`, or only whole layouts? (Lean: both,
  same column, pane-level wins.)
- Layout-level pinned params vs pane-level only (lean: pane-level only —
  layout-level is a bus write, which smells like behavior).
- Does the capture machinery (`{"op":"capture","target":"layout:crm/home"}`)
  come in P1 for visual self-check of walls? (Cheap once wall mode exists.)
