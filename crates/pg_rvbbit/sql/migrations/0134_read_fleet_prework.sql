-- 0134_read_fleet_prework.sql
-- Substrate for the read fleet (docs/READ_FLEET_PLAN.md): PUBLICATION of
-- row-group artifacts to shared object storage, distinct from the cold tier.
-- cold_url diverts the brain's own reads to object storage; published_url is
-- dual-presence — local files keep serving the brain, the published copy
-- serves remote engine workers (warrens). Plus: per-node identity on route
-- decisions, and a settings-tunable reaper grace window (must eventually
-- exceed the max remote query time so generation GC can't unlink files a
-- warren is mid-scan on).

-- Dual-presence: local `path` remains the brain's read source; published_url
-- records where the shared copy of this row group lives (NULL = unpublished).
ALTER TABLE rvbbit.row_groups ADD COLUMN IF NOT EXISTS published_url text;

-- Per-table opt-OUT. Publication is registry-wide by default once a store is
-- configured ("object store as a base primitive"); a row here with
-- enabled=false excludes a table (e.g. private data that must not leave box).
CREATE TABLE IF NOT EXISTS rvbbit.publish_policy (
    table_oid  oid PRIMARY KEY REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    enabled    boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Fleet identity on the routing breadcrumbs: which node/endpoint executed the
-- candidate. NULL = the brain's local engines (all traffic today).
ALTER TABLE rvbbit.route_decisions ADD COLUMN IF NOT EXISTS node text;

-- The one config primitive: a base URL prefix in rvbbit.settings. Non-secret
-- only — credentials stay in the server environment (AWS_*/GOOGLE_* env or
-- instance metadata; GCS via its S3-interop endpoint = AWS_ENDPOINT + HMAC).
CREATE OR REPLACE FUNCTION rvbbit.set_publish_store(url_prefix text, store_enabled boolean DEFAULT true)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO rvbbit.settings (key, value)
    VALUES ('publish_store', jsonb_build_object('url_prefix', rtrim(url_prefix, '/'), 'enabled', store_enabled))
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = clock_timestamp();
$$;

-- Publish a table's unpublished row groups to the configured store, KEEPING
-- local files and local reads (writes published_url, never cold_url). Safe to
-- re-run (only touches published_url IS NULL rows). Returns files published.
-- Called automatically at the tail of compaction (maybe_publish) and manually
-- for backfill: SELECT rvbbit.publish_row_groups('my_table');
CREATE OR REPLACE FUNCTION rvbbit.publish_row_groups(rel regclass)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    cfg        jsonb;
    prefix     text;
    rg         record;
    dest_url   text;
    published  bigint := 0;
BEGIN
    SELECT value INTO cfg FROM rvbbit.settings WHERE key = 'publish_store';
    IF cfg IS NULL OR NOT coalesce((cfg->>'enabled')::boolean, false) THEN
        RETURN 0;
    END IF;
    prefix := rtrim(cfg->>'url_prefix', '/');
    IF prefix IS NULL OR prefix = '' THEN
        RETURN 0;
    END IF;
    IF EXISTS (SELECT 1 FROM rvbbit.publish_policy
               WHERE table_oid = rel::oid AND NOT enabled) THEN
        RETURN 0;
    END IF;

    FOR rg IN
        SELECT rg_id, path
        FROM rvbbit.row_groups
        WHERE table_oid = rel::oid AND published_url IS NULL
        ORDER BY rg_id
    LOOP
        -- Mirror the artifact's local layout under the prefix so a worker's
        -- view of <prefix>/<oid>/scan/<rg_id>.parquet matches the catalog.
        dest_url := prefix || '/' || rel::oid::text || '/scan/' || rg.rg_id::text || '.parquet';
        IF dest_url LIKE 'file://%' THEN
            -- Local/NFS prefix (dev + tests): plain copy, parents created.
            -- Paths come from the rvbbit catalog (server-controlled), same
            -- trust model as 0044's migrate_to_cold file:// branch.
            EXECUTE format(
                'COPY (SELECT 1) TO PROGRAM %L',
                format('mkdir -p %s && cp %s %s',
                       regexp_replace(substr(dest_url, 8), '/[^/]*$', ''),
                       rg.path, substr(dest_url, 8))
            );
        ELSE
            PERFORM rvbbit.cold_put(rg.path, dest_url);
        END IF;
        UPDATE rvbbit.row_groups
           SET published_url = dest_url
         WHERE table_oid = rel::oid AND rg_id = rg.rg_id;
        published := published + 1;
    END LOOP;
    RETURN published;
END;
$$;

-- Publication state at a glance (feeds the freshness plane + future fleet UI):
-- per accelerated table, how much of the current generation is published.
CREATE OR REPLACE VIEW rvbbit.publish_state AS
SELECT t.table_oid,
       t.table_oid::regclass::text            AS table_name,
       count(*)                                AS row_groups,
       count(*) FILTER (WHERE rg.published_url IS NOT NULL) AS published,
       max(rg.generation)                      AS local_generation,
       coalesce(pp.enabled, true)              AS publish_enabled
FROM rvbbit.tables t
JOIN rvbbit.row_groups rg USING (table_oid)
LEFT JOIN rvbbit.publish_policy pp USING (table_oid)
GROUP BY t.table_oid, pp.enabled;
