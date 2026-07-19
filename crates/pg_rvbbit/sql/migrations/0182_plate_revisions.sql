-- 0182: Plate revisions — every destructive write to rvbbit.plates snapshots
-- the row it replaced. Plates are single rows edited in place by the
-- assistant, so before this an edit was unrecoverable (a real session lost
-- a finished dashboard to a bad restyle turn). App blocks have a revision
-- ledger; plates now do too.
--
-- Capture is a TRIGGER, not an upsert_plate edit: it catches every write
-- path (assistant upserts, kit installs, raw SQL) and leaves the
-- upsert_plate signature untouched. ENABLE ALWAYS per the 0122 lesson —
-- logical-replica apply must not silently skip the ledger.
--
-- The snapshot is the WHOLE row as jsonb, so later plates columns ride
-- along without touching this table again. restore_plate() re-materializes
-- via jsonb_populate_record; the delete-then-insert inside it fires the
-- capture trigger on the outgoing state, so a restore is itself undoable.

CREATE TABLE IF NOT EXISTS rvbbit.plate_revisions (
    plate_id    text NOT NULL,
    rev         integer NOT NULL,
    reason      text NOT NULL DEFAULT 'update',   -- update | delete
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    snapshot    jsonb NOT NULL,
    PRIMARY KEY (plate_id, rev)
);

CREATE OR REPLACE FUNCTION rvbbit.plates_capture_revision() RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_rev integer;
BEGIN
    -- Idempotent seed re-runs rewrite identical content with a fresh
    -- updated_at; those are not revisions.
    IF TG_OP = 'UPDATE'
       AND (to_jsonb(OLD) - 'updated_at') IS NOT DISTINCT FROM (to_jsonb(NEW) - 'updated_at') THEN
        RETURN NEW;
    END IF;

    SELECT coalesce(max(rev), 0) + 1 INTO v_rev
    FROM rvbbit.plate_revisions WHERE plate_id = OLD.plate_id;

    INSERT INTO rvbbit.plate_revisions (plate_id, rev, reason, snapshot)
    VALUES (OLD.plate_id, v_rev,
            CASE TG_OP WHEN 'DELETE' THEN 'delete' ELSE 'update' END,
            to_jsonb(OLD));

    -- Keep the last 20 per plate; revisions are a safety net, not an archive.
    DELETE FROM rvbbit.plate_revisions
    WHERE plate_id = OLD.plate_id AND rev < v_rev - 19;

    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END
$fn$;

DROP TRIGGER IF EXISTS plates_revision ON rvbbit.plates;
CREATE TRIGGER plates_revision
    BEFORE UPDATE OR DELETE ON rvbbit.plates
    FOR EACH ROW EXECUTE FUNCTION rvbbit.plates_capture_revision();
ALTER TABLE rvbbit.plates ENABLE ALWAYS TRIGGER plates_revision;

-- Restore a revision (latest when p_rev is NULL). Delete-then-insert so the
-- capture trigger ledgers the state being replaced — restoring is always
-- reversible. jsonb_populate_record keeps this schema-drift-proof: whatever
-- columns rvbbit.plates has at restore time are filled from the snapshot.
CREATE OR REPLACE FUNCTION rvbbit.restore_plate(
    p_plate_id text,
    p_rev      integer DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_rev  integer;
    v_snap jsonb;
BEGIN
    SELECT rev, snapshot INTO v_rev, v_snap
    FROM rvbbit.plate_revisions
    WHERE plate_id = p_plate_id AND (p_rev IS NULL OR rev = p_rev)
    ORDER BY rev DESC
    LIMIT 1;
    IF v_snap IS NULL THEN
        RAISE EXCEPTION 'no revision % for plate %',
            coalesce(p_rev::text, '(latest)'), p_plate_id;
    END IF;

    DELETE FROM rvbbit.plates WHERE plate_id = p_plate_id;
    INSERT INTO rvbbit.plates
    SELECT (jsonb_populate_record(NULL::rvbbit.plates, v_snap)).*;
    UPDATE rvbbit.plates SET updated_at = clock_timestamp()
    WHERE plate_id = p_plate_id;

    RETURN format('restored %s from rev %s (%s)', p_plate_id, v_rev,
                  v_snap->>'title');
END
$fn$;
