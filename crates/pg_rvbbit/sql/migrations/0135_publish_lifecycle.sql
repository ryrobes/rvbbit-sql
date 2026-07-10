-- 0135_publish_lifecycle.sql
-- Publication lifecycle on top of 0134: relocation (republish), local-disk
-- eviction (the "5TB on the pg data drive" escape hatch), and a storage
-- doctor. Companions to the Rust primitives cold_stat/cold_delete.
--
-- Eviction composes with what already exists rather than inventing anything:
-- verify the published copy (cold_stat size == n_bytes), point cold_url at it
-- (the read path the brain ALREADY uses when local files are absent), and
-- queue the local file through orphaned_files — the existing deferred-unlink
-- reaper, whose grace window protects any in-flight local readers. The local
-- disk becomes a working-set cache over the bucket; correctness never moves.

-- C bindings for the new primitives (idempotent; $libdir literal because
-- migrate() runs via SPI where MODULE_PATHNAME is not substituted — 0044's
-- cold_put set the precedent).
CREATE OR REPLACE FUNCTION rvbbit.cold_stat(uri text)
RETURNS bigint LANGUAGE c STRICT AS '$libdir/pg_rvbbit', 'cold_stat_wrapper';
CREATE OR REPLACE FUNCTION rvbbit.cold_delete(uri text)
RETURNS boolean LANGUAGE c STRICT AS '$libdir/pg_rvbbit', 'cold_delete_wrapper';

-- Re-home published artifacts after a publish_store change: forget stale
-- published_urls (those not under the current prefix) and re-upload from the
-- local copies. No recompaction — bytes are location-independent, only
-- catalog pointers move. Old-bucket objects are left for later cleanup
-- (double-published window; sweep with cold_delete once fleet coverage
-- confirms the new home). Returns files re-published.
CREATE OR REPLACE FUNCTION rvbbit.republish(rel regclass DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    cfg     jsonb;
    prefix  text;
    t       record;
    total   bigint := 0;
BEGIN
    SELECT value INTO cfg FROM rvbbit.settings WHERE key = 'publish_store';
    IF cfg IS NULL OR NOT coalesce((cfg->>'enabled')::boolean, false) THEN
        RAISE EXCEPTION 'rvbbit.republish: no enabled publish_store configured (rvbbit.set_publish_store)';
    END IF;
    prefix := rtrim(cfg->>'url_prefix', '/');

    -- Forget pointers that don't live under the current prefix. Evicted row
    -- groups (cold_url = old published_url) are re-pointed after re-upload.
    UPDATE rvbbit.row_groups rg
       SET published_url = NULL
     WHERE (rel IS NULL OR rg.table_oid = rel::oid)
       AND rg.published_url IS NOT NULL
       AND rg.published_url NOT LIKE prefix || '/%';

    FOR t IN
        SELECT DISTINCT rg.table_oid
        FROM rvbbit.row_groups rg
        WHERE (rel IS NULL OR rg.table_oid = rel::oid)
          AND rg.published_url IS NULL
    LOOP
        total := total + rvbbit.publish_row_groups(t.table_oid::regclass);
    END LOOP;

    -- Evicted row groups follow their published copy to the new home.
    UPDATE rvbbit.row_groups
       SET cold_url = published_url
     WHERE (rel IS NULL OR table_oid = rel::oid)
       AND published_url IS NOT NULL
       AND cold_url IS NOT NULL
       AND cold_url <> published_url;
    RETURN total;
END;
$$;

-- Evict local copies of published row groups: verify the published object
-- byte-for-byte (size), flip reads to it via cold_url, and queue the local
-- file for deferred unlink. Returns files evicted. Refuses silently (skips)
-- anything unverifiable — slow-not-wrong extends to disk reclamation.
CREATE OR REPLACE FUNCTION rvbbit.evict_local(rel regclass)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    rg      record;
    remote  bigint;
    evicted bigint := 0;
BEGIN
    FOR rg IN
        SELECT rg_id, path, n_bytes, published_url
        FROM rvbbit.row_groups
        WHERE table_oid = rel::oid
          AND published_url IS NOT NULL
          AND (cold_url IS NULL OR cold_url <> published_url)
        ORDER BY rg_id
    LOOP
        BEGIN
            remote := rvbbit.cold_stat(rg.published_url);
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'rvbbit.evict_local: % unverifiable (%) — keeping local copy', rg.published_url, SQLERRM;
            CONTINUE;
        END;
        IF remote IS DISTINCT FROM rg.n_bytes THEN
            RAISE WARNING 'rvbbit.evict_local: % size % <> catalog % — keeping local copy', rg.published_url, remote, rg.n_bytes;
            CONTINUE;
        END IF;
        UPDATE rvbbit.row_groups
           SET cold_url = rg.published_url
         WHERE table_oid = rel::oid AND rg_id = rg.rg_id;
        INSERT INTO rvbbit.orphaned_files (path, table_oid, reason)
        VALUES (rg.path, rel::oid, 'evict_local_published')
        ON CONFLICT (path) DO NOTHING;
        evicted := evicted + 1;
    END LOOP;
    RETURN evicted;
END;
$$;

-- One-call storage health report: config presence, canary write/head/delete
-- with timings. The SQL twin of the future lens Storage card; fleet_doctor
-- will fold this in.
CREATE OR REPLACE FUNCTION rvbbit.publish_store_doctor()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    cfg      jsonb;
    prefix   text;
    canary   text;
    tmp      text := '/tmp/rvbbit_store_canary';
    t0       timestamptz;
    put_ms   numeric;
    stat_ms  numeric;
    del_ms   numeric;
    size_ok  boolean;
BEGIN
    SELECT value INTO cfg FROM rvbbit.settings WHERE key = 'publish_store';
    IF cfg IS NULL THEN
        RETURN jsonb_build_object('configured', false, 'hint', 'SELECT rvbbit.set_publish_store(''s3://bucket/prefix'')');
    END IF;
    IF NOT coalesce((cfg->>'enabled')::boolean, false) THEN
        RETURN jsonb_build_object('configured', true, 'enabled', false);
    END IF;
    prefix := rtrim(cfg->>'url_prefix', '/');
    canary := prefix || '/.rvbbit-doctor/' || md5(random()::text) || '.canary';

    EXECUTE format('COPY (SELECT ''rvbbit storage canary'') TO %L', tmp);
    BEGIN
        t0 := clock_timestamp();
        PERFORM rvbbit.cold_put(tmp, canary);
        put_ms := extract(epoch FROM clock_timestamp() - t0) * 1000;

        t0 := clock_timestamp();
        size_ok := rvbbit.cold_stat(canary) > 0;
        stat_ms := extract(epoch FROM clock_timestamp() - t0) * 1000;

        t0 := clock_timestamp();
        PERFORM rvbbit.cold_delete(canary);
        del_ms := extract(epoch FROM clock_timestamp() - t0) * 1000;
    EXCEPTION WHEN OTHERS THEN
        RETURN jsonb_build_object(
            'configured', true, 'enabled', true, 'url_prefix', prefix,
            'ok', false, 'error', SQLERRM);
    END;
    RETURN jsonb_build_object(
        'configured', true, 'enabled', true, 'url_prefix', prefix,
        'ok', size_ok,
        'put_ms', round(put_ms, 1), 'head_ms', round(stat_ms, 1), 'delete_ms', round(del_ms, 1));
END;
$$;

-- publish_state grows an evicted column (local file released, reads remote).
-- DROP first: OR REPLACE can't insert a column mid-view.
DROP VIEW IF EXISTS rvbbit.publish_state;
CREATE VIEW rvbbit.publish_state AS
SELECT t.table_oid,
       t.table_oid::regclass::text            AS table_name,
       count(*)                                AS row_groups,
       count(*) FILTER (WHERE rg.published_url IS NOT NULL) AS published,
       count(*) FILTER (WHERE rg.published_url IS NOT NULL
                          AND rg.cold_url = rg.published_url) AS evicted,
       max(rg.generation)                      AS local_generation,
       coalesce(pp.enabled, true)              AS publish_enabled
FROM rvbbit.tables t
JOIN rvbbit.row_groups rg USING (table_oid)
LEFT JOIN rvbbit.publish_policy pp USING (table_oid)
GROUP BY t.table_oid, pp.enabled;
