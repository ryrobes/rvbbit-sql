-- 0184: config-table conventions live in column COMMENTs. A real session
-- watched the assistant guess a dow encoding (isodow vs 0=Sunday) because
-- nothing machine-readable said which one scheduling.hours used. The fix
-- is a convention with two halves: kits COMMENT their config columns, and
-- the assistant reads comments before assuming — and writes them when it
-- creates config tables of its own.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := '- KIT REGISTRY: when you create the FIRST plates of a new kit';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0184: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0184: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('SCHEMA CONVENTIONS: kit config tables' IN v_system) > 0 THEN
        RAISE NOTICE '0184: config comments already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0184: kit-registry anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        '- SCHEMA CONVENTIONS: kit config tables document their conventions with column COMMENTs (e.g. whether a dow column means ISO 1=Monday or 0=Sunday, what a status enum''s values are). Before assuming any encoding, read them: select col_description(''schema.table''::regclass, attnum). When YOU create a config table, add COMMENT ON COLUMN for every non-obvious convention — later turns only know what the catalog says.'
        || E'\n' || v_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0184: config comments taught (% chars)', length(v_system);
END
$patch$;
