-- 0186: patch_plate + incremental plate authoring. A real session watched
-- two consecutive calendar-plate turns die as "could not finish a valid
-- desktop command" — a Google-Calendar-sized plate is one giant JSON
-- string, and giant single commands fail two ways (output overflow, string
-- escaping fumbles). The fix is structural: plates are AUTHORED
-- INCREMENTALLY (skeleton via upsert_plate, remainder via patch_plate),
-- and routine edits send only what changed. The lens pairs this with an
-- auto-repair turn that feeds the parse diagnosis back on failure.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_cmd_anchor text := '{"op":"open_plate","plate_id":"team/my_surface"}';
    v_doc_anchor text := '- SCHEMA CONVENTIONS: kit config tables';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0186: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0186: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('patch_plate' IN v_system) > 0 THEN
        RAISE NOTICE '0186: patch_plate already taught; skipping';
        RETURN;
    END IF;
    IF position(v_cmd_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0186: open_plate command anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_doc_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0186: schema-conventions anchor not found — prompt drifted, re-author';
    END IF;

    -- 1. The op, in the command list right before open_plate.
    v_system := replace(v_system, v_cmd_anchor,
        '{"op":"patch_plate","plate_id":"team/my_surface","queries":{"aging":{"sql":"SELECT ..."},"old_probe":null},"actions":{"mark_paid":{"sql":"UPDATE ...","args":[{"name":"id","type":"text"}]}}},'
        || E'\n    ' || v_cmd_anchor);

    -- 2. The doctrine, as a PLATES-section bullet.
    v_system := replace(v_system, v_doc_anchor,
        '- LARGE PLATES ARE BUILT INCREMENTALLY: one giant upsert_plate command fails two ways (output overflow, JSON-escaping mistakes in a huge template string). For anything beyond a modest surface, first upsert_plate a WORKING SKELETON — the full template plus only the queries it already references — then add the remaining queries/actions with patch_plate commands in the same turn or follow-up turns. patch_plate updates an EXISTING plate: template/title/description/kit replace when present, queries/actions MERGE PER KEY (a null value removes the key), params replaces whole. Routine edits use patch_plate too — changing one query sends ONE query, never the whole plate. Every patch is ledgered in plate_revisions, so incremental authoring is always recoverable.'
        || E'\n' || v_doc_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0186: patch_plate taught (% chars)', length(v_system);
END
$patch$;
