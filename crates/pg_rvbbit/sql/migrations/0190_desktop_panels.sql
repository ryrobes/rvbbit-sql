-- 0190: the DataRabbit panel registry — the assistant-as-help-system table.
--
-- The lens syncs its launcher registry (the same list that powers the ⌘P
-- command palette) into this table on connect: every panel's id, label,
-- description, folder, supported deep-link hints, and a notes line about
-- what's inside. The desktop assistant queries it ON DEMAND when someone
-- asks "where do I see X?" and drives the open_panel command with the
-- result — zero per-turn context cost, and the help index maintains
-- itself because it IS the palette metadata.
--
-- Rows are lens-owned and version-specific: sync upserts and prunes, so
-- the table always matches the running app. Not surfaced to warehouse MCP
-- clients (rvbbit internals are schema-scoped away there already).

CREATE TABLE IF NOT EXISTS rvbbit.desktop_panels (
    id          text PRIMARY KEY,
    label       text NOT NULL,
    description text,
    folder      text,
    -- Deep-link hint values open_panel accepts for this panel, e.g.
    -- system-objects: ["tables","indexes","locks",...]. Empty = none.
    hints       jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- What's inside (sections/tabs) — so the assistant can give directions
    -- even where no deep-link exists.
    notes       text,
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT desktop_panels_hints_is_array CHECK (jsonb_typeof(hints) = 'array')
);

COMMENT ON TABLE rvbbit.desktop_panels IS
    'DataRabbit desktop panel registry, synced from the running lens at connect. The desktop assistant queries it to answer where-do-I-find-X questions and to drive the open_panel command.';
