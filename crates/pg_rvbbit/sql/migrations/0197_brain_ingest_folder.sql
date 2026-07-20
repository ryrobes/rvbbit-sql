-- 0197: rvbbit.brain_ingest_folder — the SQL-native "eat this folder".
--
-- The minimal document-brain path is now: install the extension, drop files
-- somewhere Postgres can read (the compose ships a shared /staging volume),
-- run ONE function, search the combined KG. Text/markdown ingests with the
-- extension alone (embeddings via the in-process local_embed transport);
-- binary formats (pdf/docx/xlsx/pptx/images) route through the extract_doc
-- operator when its sidecar is up — per-file failures are collected, never
-- fatal. Re-runs are idempotent: brain_ingest upserts on (source, uri) and
-- uri here is the file path.
--
-- Filesystem access uses pg_ls_dir/pg_stat_file/pg_read_file — superuser
-- (or pg_read_server_files) only, which is the right wall: this reads the
-- DATABASE HOST's filesystem. Binary extraction additionally requires the
-- path to live under the volume the doc-extract sidecar shares (/staging
-- in the shipped compose).

CREATE OR REPLACE FUNCTION rvbbit.brain_ingest_folder(
    p_dir       text,
    p_source    text DEFAULT NULL,
    p_roles     text[] DEFAULT NULL,
    p_recursive boolean DEFAULT true,
    p_max_files integer DEFAULT 500
) RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_source    text := coalesce(nullif(btrim(p_source), ''), regexp_replace(rtrim(p_dir, '/'), '^.*/', ''));
    v_root      text := rtrim(p_dir, '/');
    v_queue     text[] := ARRAY[rtrim(p_dir, '/')];
    v_dir       text;
    v_entry     text;
    v_path      text;
    v_rel       text;
    v_ext       text;
    v_mime      text;
    v_body      text;
    v_stat      record;
    v_ingested  integer := 0;
    v_extracted integer := 0;
    v_skipped   integer := 0;
    v_errors    jsonb := '[]'::jsonb;
    v_cap       integer := greatest(1, least(coalesce(p_max_files, 500), 5000));
    v_has_extract boolean := EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'rvbbit' AND p.proname = 'extract_doc');
BEGIN
    IF v_root IS NULL OR v_root = '' THEN
        RAISE EXCEPTION 'brain_ingest_folder: p_dir required';
    END IF;

    WHILE array_length(v_queue, 1) > 0 AND v_ingested < v_cap LOOP
        v_dir := v_queue[1];
        v_queue := v_queue[2:];
        FOR v_entry IN SELECT pg_ls_dir(v_dir) ORDER BY 1 LOOP
            EXIT WHEN v_ingested >= v_cap;
            v_path := v_dir || '/' || v_entry;
            BEGIN
                SELECT * INTO v_stat FROM pg_stat_file(v_path);
                IF v_stat.isdir THEN
                    IF p_recursive AND v_entry NOT LIKE '.%' THEN
                        v_queue := v_queue || v_path;
                    END IF;
                    CONTINUE;
                END IF;
                v_ext := lower(regexp_replace(v_entry, '^.*\.', ''));
                v_rel := ltrim(replace(left(v_path, greatest(length(v_path) - length(v_entry) - 1, 0)), v_root, ''), '/');

                IF v_ext IN ('md','markdown','mdx','txt','text','rst','org','log','csv','json','html','htm') THEN
                    IF v_stat.size > 2000000 THEN
                        v_skipped := v_skipped + 1;
                        CONTINUE;
                    END IF;
                    v_body := pg_read_file(v_path);
                ELSIF v_has_extract AND v_ext IN ('pdf','docx','xlsx','pptx','doc','rtf','png','jpg','jpeg','tiff','webp') THEN
                    v_mime := CASE v_ext
                        WHEN 'pdf'  THEN 'application/pdf'
                        WHEN 'docx' THEN 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                        WHEN 'xlsx' THEN 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                        WHEN 'pptx' THEN 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
                        WHEN 'doc'  THEN 'application/msword'
                        WHEN 'rtf'  THEN 'application/rtf'
                        WHEN 'png'  THEN 'image/png'
                        WHEN 'tiff' THEN 'image/tiff'
                        WHEN 'webp' THEN 'image/webp'
                        ELSE 'image/jpeg' END;
                    v_body := nullif(rvbbit.extract_doc(v_path, v_mime), '');
                    IF v_body IS NULL THEN
                        v_skipped := v_skipped + 1;
                        CONTINUE;
                    END IF;
                    v_extracted := v_extracted + 1;
                ELSE
                    v_skipped := v_skipped + 1;
                    CONTINUE;
                END IF;

                PERFORM rvbbit.brain_ingest(
                    p_source => v_source,
                    p_title  => v_entry,
                    p_body   => v_body,
                    p_roles  => p_roles,
                    p_folder => '/' || v_source || CASE WHEN v_rel = '' THEN '' ELSE '/' || v_rel END,
                    p_uri    => v_path);
                v_ingested := v_ingested + 1;
            EXCEPTION WHEN OTHERS THEN
                v_errors := v_errors || jsonb_build_object('file', v_path, 'error', SQLERRM);
            END;
        END LOOP;
    END LOOP;

    RETURN jsonb_build_object(
        'source', v_source,
        'ingested', v_ingested,
        'extracted', v_extracted,
        'skipped', v_skipped,
        'errors', v_errors,
        'capped', v_ingested >= v_cap);
END
$fn$;

COMMENT ON FUNCTION rvbbit.brain_ingest_folder(text, text, text[], boolean, integer) IS
    'Ingest a Postgres-readable folder into the document brain: text/markdown directly, binary formats via extract_doc when its sidecar is up (paths under the shared /staging volume). Idempotent per file path. Superuser/pg_read_server_files only.';
