-- =====================================================================
-- Catalog Drift: diff fingerprint snapshots across crawl runs.
-- See docs/CATALOG_KG_PLAN.md §11.
--
-- Read-only over rvbbit.catalog_snapshots / rvbbit.catalog_runs (written by
-- rvbbit.catalog_crawl in catalog_kg.sql). Idempotent; psql -f loadable and
-- compiled into the extension via src/catalog_drift.rs.
-- =====================================================================

-- Idempotency for reloads where parameter names changed (CREATE OR REPLACE
-- cannot rename input parameters). Harmless on a fresh install.
DROP FUNCTION IF EXISTS rvbbit.catalog_cosine(real[], real[]);
DROP FUNCTION IF EXISTS rvbbit.catalog_object_history(text, text);

-- ---------------------------------------------------------------------
-- Run helpers
-- ---------------------------------------------------------------------

-- Runs for a graph, newest first, with snapshot counts (for the run picker).
CREATE OR REPLACE FUNCTION rvbbit.catalog_runs_list(
    graph   text DEFAULT 'db_catalog',
    limit_n int  DEFAULT 50)
RETURNS TABLE (
    run_id       bigint,
    status       text,
    started_at   timestamptz,
    finished_at  timestamptz,
    tables_seen  bigint,
    columns_seen bigint,
    snapshots    bigint)
LANGUAGE sql STABLE AS $fn$
    SELECT r.run_id, r.status, r.started_at, r.finished_at,
           r.tables_seen, r.columns_seen,
           (SELECT count(*) FROM rvbbit.catalog_snapshots s WHERE s.run_id = r.run_id)
    FROM rvbbit.catalog_runs r
    WHERE r.graph_id = COALESCE(NULLIF(btrim(graph), ''), 'db_catalog')
    ORDER BY r.run_id DESC
    LIMIT GREATEST(1, COALESCE(limit_n, 50))
$fn$;

-- Nearest finished run at-or-before a timestamp (date-vs-date drift).
CREATE OR REPLACE FUNCTION rvbbit.catalog_run_at(
    graph text,
    ts    timestamptz)
RETURNS bigint
LANGUAGE sql STABLE AS $fn$
    SELECT run_id
    FROM rvbbit.catalog_runs
    WHERE graph_id = COALESCE(NULLIF(btrim(graph), ''), 'db_catalog')
      AND status = 'ok'
      AND COALESCE(finished_at, started_at) <= ts
    ORDER BY COALESCE(finished_at, started_at) DESC
    LIMIT 1
$fn$;

-- Cosine between two embedding arrays (rvbbit.cosine is not SQL-exposed).
-- A dedicated function so record fields can be passed as args (plpgsql cannot
-- reliably substitute record fields inside a multi-arg unnest() in FROM).
CREATE OR REPLACE FUNCTION rvbbit.catalog_cosine(va real[], vb real[])
RETURNS float8
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    v float8;
    n int;
BEGIN
    n := array_length(va, 1);
    IF va IS NULL OR vb IS NULL OR n IS NULL
       OR n IS DISTINCT FROM array_length(vb, 1) THEN
        RETURN NULL;
    END IF;
    -- Index the arrays by subscript; multi-arg unnest(var, var) does not
    -- substitute plpgsql variables reliably, so avoid it here.
    SELECT sum(va[i] * vb[i])
           / NULLIF(sqrt(sum(va[i] * va[i])) * sqrt(sum(vb[i] * vb[i])), 0)
      INTO v
      FROM generate_series(1, n) AS i;
    RETURN v;
END $fn$;

-- ---------------------------------------------------------------------
-- Value-distribution drift: new/lost values + PSI between two value_dist maps
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.catalog_value_drift(
    dist_a jsonb,
    dist_b jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $fn$
    WITH a AS (SELECT key, (value)::numeric AS n FROM jsonb_each_text(COALESCE(dist_a, '{}'::jsonb))),
         b AS (SELECT key, (value)::numeric AS n FROM jsonb_each_text(COALESCE(dist_b, '{}'::jsonb))),
         ta AS (SELECT NULLIF(COALESCE(sum(n), 0), 0) AS t FROM a),
         tb AS (SELECT NULLIF(COALESCE(sum(n), 0), 0) AS t FROM b),
         keys AS (SELECT key FROM a UNION SELECT key FROM b),
         buckets AS (
             SELECT k.key,
                    COALESCE((SELECT n FROM a WHERE a.key = k.key), 0) AS na,
                    COALESCE((SELECT n FROM b WHERE b.key = k.key), 0) AS nb
             FROM keys k
         ),
         psi AS (
             SELECT sum(
                      (((nb) / (SELECT t FROM tb)) - ((na) / (SELECT t FROM ta)))
                      * ln((((nb) / (SELECT t FROM tb)) + 1e-6)
                           / (((na) / (SELECT t FROM ta)) + 1e-6))
                    ) AS psi
             FROM buckets
             WHERE (SELECT t FROM ta) IS NOT NULL AND (SELECT t FROM tb) IS NOT NULL
         )
    SELECT jsonb_build_object(
        'new_values',  COALESCE((SELECT jsonb_agg(key ORDER BY key)
                                   FROM b WHERE key NOT IN (SELECT key FROM a)), '[]'::jsonb),
        'lost_values', COALESCE((SELECT jsonb_agg(key ORDER BY key)
                                   FROM a WHERE key NOT IN (SELECT key FROM b)), '[]'::jsonb),
        'psi',         (SELECT round(psi::numeric, 4) FROM psi))
$fn$;

-- ---------------------------------------------------------------------
-- Per-object drift between two runs
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.catalog_drift(
    run_a        bigint,
    run_b        bigint,
    graph        text    DEFAULT 'db_catalog',
    only_changed boolean DEFAULT true)
RETURNS TABLE (
    obj_key     text,
    kind        text,
    schema_name text,
    rel_name    text,
    col_name    text,
    change_type text,
    severity    float8,
    flags       text[],
    diff        jsonb)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_graph text := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    r       record;
    v_flags text[];
    v_sev   float8;
    v_diff  jsonb;
    v_ct    text;
    v_embed float8;
    v_vd    jsonb;
    a_rows  numeric; b_rows numeric;
    a_nf    numeric; b_nf   numeric;
    a_ndv   numeric; b_ndv  numeric;
    n_new   int;     n_lost int;
    v_psi   numeric;
BEGIN
    FOR r IN
        SELECT COALESCE(a.obj_key, b.obj_key)         AS obj_key,
               COALESCE(a.kind, b.kind)               AS kind,
               COALESCE(a.schema_name, b.schema_name) AS schema_name,
               COALESCE(a.rel_name, b.rel_name)       AS rel_name,
               COALESCE(a.col_name, b.col_name)       AS col_name,
               a.fingerprint AS fa, b.fingerprint AS fb,
               a.embedding   AS ea, b.embedding   AS eb,
               (a.obj_key IS NOT NULL) AS in_a,
               (b.obj_key IS NOT NULL) AS in_b
        FROM (SELECT * FROM rvbbit.catalog_snapshots WHERE run_id = run_a AND graph_id = v_graph) a
        FULL OUTER JOIN
             (SELECT * FROM rvbbit.catalog_snapshots WHERE run_id = run_b AND graph_id = v_graph) b
          ON a.obj_key = b.obj_key
    LOOP
        v_flags := ARRAY[]::text[];
        v_sev   := 0;
        v_diff  := '{}'::jsonb;

        IF NOT r.in_a THEN
            v_ct := 'added';
            v_flags := array_append(v_flags, 'added');
            v_sev := 0.8;
        ELSIF NOT r.in_b THEN
            v_ct := 'dropped';
            v_flags := array_append(v_flags, 'dropped');
            v_sev := 0.9;
        ELSE
            v_ct := 'unchanged';

            -- type
            IF (r.fa->>'data_type') IS DISTINCT FROM (r.fb->>'data_type') THEN
                v_diff := v_diff || jsonb_build_object('data_type',
                    jsonb_build_object('a', r.fa->>'data_type', 'b', r.fb->>'data_type'));
                v_flags := array_append(v_flags, 'type_change');
                v_sev := greatest(v_sev, 0.9); v_ct := 'changed';
            END IF;

            -- nullable
            IF (r.fa->>'nullable') IS DISTINCT FROM (r.fb->>'nullable') THEN
                v_diff := v_diff || jsonb_build_object('nullable',
                    jsonb_build_object('a', (r.fa->>'nullable'), 'b', (r.fb->>'nullable')));
                v_flags := array_append(v_flags,
                    CASE WHEN (r.fb->>'nullable') = 'true' THEN 'became_nullable' ELSE 'became_not_null' END);
                v_sev := greatest(v_sev, 0.5); v_ct := 'changed';
            END IF;

            -- pk / fk / fk_target
            IF (r.fa->>'is_pk') IS DISTINCT FROM (r.fb->>'is_pk') THEN
                v_diff := v_diff || jsonb_build_object('is_pk',
                    jsonb_build_object('a', (r.fa->>'is_pk'), 'b', (r.fb->>'is_pk')));
                v_flags := array_append(v_flags, 'pk_change');
                v_sev := greatest(v_sev, 0.6); v_ct := 'changed';
            END IF;
            IF (r.fa->>'fk_target') IS DISTINCT FROM (r.fb->>'fk_target') THEN
                v_diff := v_diff || jsonb_build_object('fk_target',
                    jsonb_build_object('a', (r.fa->>'fk_target'), 'b', (r.fb->>'fk_target')));
                v_flags := array_append(v_flags, 'fk_change');
                v_sev := greatest(v_sev, 0.55); v_ct := 'changed';
            END IF;

            -- comment
            IF (r.fa->>'comment') IS DISTINCT FROM (r.fb->>'comment') THEN
                v_diff := v_diff || jsonb_build_object('comment',
                    jsonb_build_object('a', r.fa->>'comment', 'b', r.fb->>'comment'));
                v_flags := array_append(v_flags, 'comment_change');
                v_sev := greatest(v_sev, 0.15); v_ct := 'changed';
            END IF;

            -- row count (tables)
            a_rows := (r.fa->>'n_rows')::numeric;
            b_rows := (r.fb->>'n_rows')::numeric;
            IF a_rows IS DISTINCT FROM b_rows AND (a_rows IS NOT NULL OR b_rows IS NOT NULL) THEN
                v_diff := v_diff || jsonb_build_object('n_rows', jsonb_build_object(
                    'a', a_rows, 'b', b_rows, 'delta', COALESCE(b_rows, 0) - COALESCE(a_rows, 0),
                    'pct', CASE WHEN COALESCE(a_rows, 0) > 0
                                THEN round((b_rows - a_rows) / a_rows * 100, 1) ELSE NULL END));
                v_flags := array_append(v_flags,
                    CASE WHEN COALESCE(b_rows, 0) > COALESCE(a_rows, 0) THEN 'rows_up' ELSE 'rows_down' END);
                v_sev := greatest(v_sev,
                    least(0.5, abs(COALESCE(b_rows, 0) - COALESCE(a_rows, 0)) / GREATEST(COALESCE(a_rows, 1), 1)));
                v_ct := 'changed';
            END IF;

            -- ndv
            a_ndv := (r.fa->>'ndv')::numeric;
            b_ndv := (r.fb->>'ndv')::numeric;
            IF a_ndv IS DISTINCT FROM b_ndv AND (a_ndv IS NOT NULL OR b_ndv IS NOT NULL) THEN
                v_diff := v_diff || jsonb_build_object('ndv', jsonb_build_object('a', a_ndv, 'b', b_ndv));
                v_flags := array_append(v_flags,
                    CASE WHEN COALESCE(b_ndv, 0) > COALESCE(a_ndv, 0) THEN 'ndv_up' ELSE 'ndv_down' END);
                v_sev := greatest(v_sev, 0.3); v_ct := 'changed';
            END IF;

            -- null fraction
            a_nf := (r.fa->>'null_frac')::numeric;
            b_nf := (r.fb->>'null_frac')::numeric;
            IF a_nf IS DISTINCT FROM b_nf AND (a_nf IS NOT NULL OR b_nf IS NOT NULL) THEN
                v_diff := v_diff || jsonb_build_object('null_frac', jsonb_build_object(
                    'a', a_nf, 'b', b_nf, 'delta', round((COALESCE(b_nf, 0) - COALESCE(a_nf, 0))::numeric, 4)));
                IF COALESCE(b_nf, 0) - COALESCE(a_nf, 0) >= 0.1 THEN
                    v_flags := array_append(v_flags, 'null_spike');
                    v_sev := greatest(v_sev, least(0.85, (b_nf - a_nf) * 2));
                END IF;
                v_ct := 'changed';
            END IF;

            -- min/max range
            IF (r.fa->>'min') IS DISTINCT FROM (r.fb->>'min')
               OR (r.fa->>'max') IS DISTINCT FROM (r.fb->>'max') THEN
                v_diff := v_diff || jsonb_build_object('range', jsonb_build_object(
                    'min_a', r.fa->>'min', 'min_b', r.fb->>'min',
                    'max_a', r.fa->>'max', 'max_b', r.fb->>'max'));
                v_flags := array_append(v_flags, 'range_shift');
                v_sev := greatest(v_sev, 0.2); v_ct := 'changed';
            END IF;

            -- categorical value drift — only when BOTH sides captured the full
            -- value set. For high-cardinality columns value_dist is capped, so
            -- new/lost/PSI would just be sampling noise; ndv + embedding drift
            -- carry the signal there instead.
            IF r.kind = 'db_column'
               AND (r.fa->>'value_dist_complete') = 'true'
               AND (r.fb->>'value_dist_complete') = 'true' THEN
                v_vd := rvbbit.catalog_value_drift(r.fa->'value_dist', r.fb->'value_dist');
                n_new  := jsonb_array_length(COALESCE(v_vd->'new_values', '[]'::jsonb));
                n_lost := jsonb_array_length(COALESCE(v_vd->'lost_values', '[]'::jsonb));
                v_psi  := NULLIF(v_vd->>'psi', '')::numeric;
                IF n_new > 0 THEN
                    v_flags := array_append(v_flags, 'new_values');
                    v_sev := greatest(v_sev, 0.6); v_ct := 'changed';
                END IF;
                IF n_lost > 0 THEN
                    v_flags := array_append(v_flags, 'lost_values');
                    v_sev := greatest(v_sev, 0.4); v_ct := 'changed';
                END IF;
                IF v_psi IS NOT NULL AND v_psi >= 0.25 THEN
                    v_flags := array_append(v_flags, 'dist_shift');
                    v_sev := greatest(v_sev, least(0.8, v_psi)); v_ct := 'changed';
                END IF;
                IF n_new > 0 OR n_lost > 0 OR v_psi IS NOT NULL THEN
                    v_diff := v_diff || jsonb_build_object('values', v_vd);
                END IF;
            END IF;

            -- embedding drift = 1 - cosine
            IF r.ea IS NOT NULL AND r.eb IS NOT NULL THEN
                v_embed := 1.0 - rvbbit.catalog_cosine(r.ea, r.eb);
                IF v_embed IS NOT NULL THEN
                    v_diff := v_diff || jsonb_build_object('embed_drift', round(v_embed::numeric, 4));
                    IF v_embed >= 0.12 THEN
                        v_flags := array_append(v_flags, 'embed_drift');
                        v_sev := greatest(v_sev, least(0.7, v_embed * 3));
                        v_ct := 'changed';
                    END IF;
                END IF;
            END IF;
        END IF;

        IF only_changed AND v_ct = 'unchanged' THEN
            CONTINUE;
        END IF;

        obj_key     := r.obj_key;
        kind        := r.kind;
        schema_name := r.schema_name;
        rel_name    := r.rel_name;
        col_name    := r.col_name;
        change_type := v_ct;
        severity    := round(v_sev::numeric, 3)::float8;
        flags       := v_flags;
        diff        := v_diff;
        RETURN NEXT;
    END LOOP;
END $fn$;

-- ---------------------------------------------------------------------
-- Rollup summary for the drift window's header band
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.catalog_drift_summary(
    run_a bigint,
    run_b bigint,
    graph text DEFAULT 'db_catalog')
RETURNS jsonb
LANGUAGE sql STABLE AS $fn$
    WITH d AS (SELECT * FROM rvbbit.catalog_drift(run_a, run_b, graph, true))
    SELECT jsonb_build_object(
        'total',    (SELECT count(*) FROM d),
        'added',    (SELECT count(*) FROM d WHERE change_type = 'added'),
        'dropped',  (SELECT count(*) FROM d WHERE change_type = 'dropped'),
        'changed',  (SELECT count(*) FROM d WHERE change_type = 'changed'),
        'tables',   (SELECT count(*) FROM d WHERE kind = 'db_table'),
        'columns',  (SELECT count(*) FROM d WHERE kind = 'db_column'),
        'max_severity', (SELECT COALESCE(max(severity), 0) FROM d),
        'flags',    COALESCE((SELECT jsonb_object_agg(f, c)
                                FROM (SELECT unnest(flags) AS f, count(*) AS c
                                        FROM d GROUP BY 1) z), '{}'::jsonb))
$fn$;

-- ---------------------------------------------------------------------
-- Metric history for one object across all runs (sparklines)
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.catalog_object_history(
    p_graph   text,
    p_obj_key text)
RETURNS TABLE (
    run_id      bigint,
    captured_at timestamptz,
    n_rows      numeric,
    ndv         numeric,
    null_frac   numeric)
LANGUAGE sql STABLE AS $fn$
    SELECT s.run_id, s.captured_at,
           (s.fingerprint->>'n_rows')::numeric,
           (s.fingerprint->>'ndv')::numeric,
           (s.fingerprint->>'null_frac')::numeric
    FROM rvbbit.catalog_snapshots s
    WHERE s.graph_id = COALESCE(NULLIF(btrim(p_graph), ''), 'db_catalog')
      AND s.obj_key = p_obj_key
    ORDER BY s.run_id
$fn$;
