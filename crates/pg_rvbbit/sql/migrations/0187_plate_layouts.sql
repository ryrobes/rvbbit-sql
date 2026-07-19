-- 0187: Layouts — the plate compose layer (docs/PLATE_COMPOSE_PLAN.md).
--
-- A layout is a named, kit-shipped composition of existing plates on a
-- free-floating canvas: pane rects stored as FRACTIONS of a declared
-- design size, plus z-order. Doctrine: a layout owns arrangement, never
-- behavior — no buttons, no actions, no expressions here, ever. Header
-- bars are plates; orchestration is the param bus; modals are windows.
--
-- Pane shape (jsonb array entries):
--   {"id":"inspector", "plate":"crm/customer-card",
--    "x":0.62,"y":0.08,"w":0.36,"h":0.55,"z":2,
--    "params":{"tab":"activity"},   -- pinned params, merged under the bus
--    "slot":true,                   -- renders empty until rv-open @pane
--    "title":"Customer"}            -- optional hover-pill label

CREATE TABLE IF NOT EXISTS rvbbit.plate_layouts (
    layout_id     text PRIMARY KEY,
    kit           text,
    title         text NOT NULL,
    description   text,
    requires_role text,
    design        jsonb NOT NULL DEFAULT '{"width":1600,"height":900}'::jsonb,
    panes         jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT plate_layouts_design_is_object CHECK (jsonb_typeof(design) = 'object'),
    CONSTRAINT plate_layouts_panes_is_array  CHECK (jsonb_typeof(panes)  = 'array')
);

-- The kit's front door: the kit icon/launcher opens this layout.
ALTER TABLE rvbbit.kits ADD COLUMN IF NOT EXISTS default_layout text;

CREATE OR REPLACE FUNCTION rvbbit.upsert_layout(
    p_layout_id   text,
    p_title       text,
    p_design      jsonb,
    p_panes       jsonb,
    p_kit         text DEFAULT NULL,
    p_description text DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
AS $up$
DECLARE
    pane jsonb;
    ids  text[] := '{}';
    pid  text;
    frac text;
    v    numeric;
BEGIN
    IF btrim(coalesce(p_layout_id, '')) = '' THEN
        RAISE EXCEPTION 'layout_id required';
    END IF;
    IF btrim(coalesce(p_title, '')) = '' THEN
        RAISE EXCEPTION 'title required';
    END IF;
    IF jsonb_typeof(p_design) IS DISTINCT FROM 'object'
       OR coalesce((p_design->>'width')::numeric, 0) <= 0
       OR coalesce((p_design->>'height')::numeric, 0) <= 0 THEN
        RAISE EXCEPTION 'design must be an object with positive width and height';
    END IF;
    IF jsonb_typeof(p_panes) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'panes must be an array';
    END IF;

    FOR pane IN SELECT * FROM jsonb_array_elements(p_panes) LOOP
        pid := btrim(coalesce(pane->>'id', ''));
        IF pid = '' THEN
            RAISE EXCEPTION 'every pane needs an id';
        END IF;
        IF pid = ANY(ids) THEN
            RAISE EXCEPTION 'duplicate pane id %', pid;
        END IF;
        ids := ids || pid;
        -- A pane is a plate reference, a slot, or a slot with a default
        -- occupant. It is never behavior.
        IF btrim(coalesce(pane->>'plate', '')) = ''
           AND coalesce((pane->>'slot')::boolean, false) IS NOT TRUE THEN
            RAISE EXCEPTION 'pane % needs a plate or "slot": true', pid;
        END IF;
        FOREACH frac IN ARRAY ARRAY['x','y','w','h'] LOOP
            v := (pane->>frac)::numeric;
            IF v IS NULL OR v < 0 OR v > 1 THEN
                RAISE EXCEPTION 'pane % needs % as a fraction in [0,1]', pid, frac;
            END IF;
        END LOOP;
        IF (pane->>'w')::numeric = 0 OR (pane->>'h')::numeric = 0 THEN
            RAISE EXCEPTION 'pane % needs nonzero w and h', pid;
        END IF;
        IF pane ? 'params' AND jsonb_typeof(pane->'params') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'pane % params must be an object', pid;
        END IF;
        -- The HyperCard wall, enforced at install: arrangement keys only.
        IF EXISTS (
            SELECT 1 FROM jsonb_object_keys(pane) k
            WHERE k NOT IN ('id','plate','x','y','w','h','z','params','slot','title')
        ) THEN
            RAISE EXCEPTION 'pane % carries a non-arrangement key (layouts own arrangement, never behavior)', pid;
        END IF;
    END LOOP;

    INSERT INTO rvbbit.plate_layouts AS l
        (layout_id, kit, title, description, design, panes)
    VALUES
        (p_layout_id, p_kit, p_title, p_description, p_design, p_panes)
    ON CONFLICT (layout_id) DO UPDATE SET
        kit = EXCLUDED.kit,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        design = EXCLUDED.design,
        panes = EXCLUDED.panes,
        updated_at = clock_timestamp();

    RETURN p_layout_id;
END
$up$;

-- Revisions: same safety net as plates (0182) — whole-row jsonb snapshots
-- on content change, last 20, restore is undoable.
CREATE TABLE IF NOT EXISTS rvbbit.plate_layout_revisions (
    layout_id   text NOT NULL,
    rev         integer NOT NULL,
    reason      text NOT NULL DEFAULT 'update',
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    snapshot    jsonb NOT NULL,
    PRIMARY KEY (layout_id, rev)
);

CREATE OR REPLACE FUNCTION rvbbit.plate_layouts_capture_revision() RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_rev integer;
BEGIN
    IF TG_OP = 'UPDATE'
       AND (to_jsonb(OLD) - 'updated_at') IS NOT DISTINCT FROM (to_jsonb(NEW) - 'updated_at') THEN
        RETURN NEW;
    END IF;
    SELECT coalesce(max(rev), 0) + 1 INTO v_rev
    FROM rvbbit.plate_layout_revisions WHERE layout_id = OLD.layout_id;
    INSERT INTO rvbbit.plate_layout_revisions (layout_id, rev, reason, snapshot)
    VALUES (OLD.layout_id, v_rev,
            CASE TG_OP WHEN 'DELETE' THEN 'delete' ELSE 'update' END,
            to_jsonb(OLD));
    DELETE FROM rvbbit.plate_layout_revisions
    WHERE layout_id = OLD.layout_id AND rev < v_rev - 19;
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END
$fn$;

DROP TRIGGER IF EXISTS plate_layouts_revision ON rvbbit.plate_layouts;
CREATE TRIGGER plate_layouts_revision
    BEFORE UPDATE OR DELETE ON rvbbit.plate_layouts
    FOR EACH ROW EXECUTE FUNCTION rvbbit.plate_layouts_capture_revision();
ALTER TABLE rvbbit.plate_layouts ENABLE ALWAYS TRIGGER plate_layouts_revision;

CREATE OR REPLACE FUNCTION rvbbit.restore_layout(
    p_layout_id text,
    p_rev       integer DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_rev  integer;
    v_snap jsonb;
BEGIN
    SELECT rev, snapshot INTO v_rev, v_snap
    FROM rvbbit.plate_layout_revisions
    WHERE layout_id = p_layout_id AND (p_rev IS NULL OR rev = p_rev)
    ORDER BY rev DESC
    LIMIT 1;
    IF v_snap IS NULL THEN
        RAISE EXCEPTION 'no revision % for layout %',
            coalesce(p_rev::text, '(latest)'), p_layout_id;
    END IF;
    DELETE FROM rvbbit.plate_layouts WHERE layout_id = p_layout_id;
    INSERT INTO rvbbit.plate_layouts
    SELECT (jsonb_populate_record(NULL::rvbbit.plate_layouts, v_snap)).*;
    UPDATE rvbbit.plate_layouts SET updated_at = clock_timestamp()
    WHERE layout_id = p_layout_id;
    RETURN format('restored %s from rev %s (%s)', p_layout_id, v_rev, v_snap->>'title');
END
$fn$;
