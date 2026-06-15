-- 0035_fast_fingerprint_materialize_sample
--
-- catalog_fingerprint_table pointed every per-column stat at a re-evaluated
-- (TABLESAMPLE …) / (… LIMIT) subquery, so for each column it re-ran the sample
-- ~5× (count, count(distinct), min/max, value_dist group-by, 5 percentile sorts).
-- On a wide/large table that re-scanned a multi-GB relation N_columns×5 times
-- (and re-executed views per column) — the dominant cost of the whole crawl
-- once embedding was fixed (e.g. a 1M-row/318MB table took ~100s+).
--
-- Fix: materialize the working set ONCE into a TEMP table and run all per-column
-- passes against that small local table. Output shape is unchanged. (Carried as
-- a migration so already-installed DBs get it via rvbbit.migrate(), since the
-- pgrx extension-SQL upgrade path is blocked on this prod DB.)

CREATE OR REPLACE FUNCTION rvbbit.catalog_fingerprint_table(
    rel regclass,
    sample_rows int DEFAULT 50000,
    examples_k  int DEFAULT 12)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_schema   text;
    v_table    text;
    v_relkind  "char";
    v_comment  text;
    v_size     bigint;
    v_nrows    bigint;
    v_nsampled bigint;
    v_sampled  boolean := false;
    v_src      text;
    v_pct      numeric;
    v_columns  jsonb := '[]'::jsonb;
    rc         record;
    v_seen     bigint;
    v_nonnull  bigint;
    v_nulls    bigint;
    v_nullfrac float8;
    v_ndv      bigint;
    v_min      text;
    v_max      text;
    v_examples jsonb;
    v_dist     jsonb;
    v_quantiles jsonb;
    distinct_cap int := 256;
BEGIN
    SELECT n.nspname, c.relname, c.relkind,
           obj_description(c.oid, 'pg_class'),
           pg_total_relation_size(c.oid)
      INTO v_schema, v_table, v_relkind, v_comment, v_size
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid = rel;

    EXECUTE format('SELECT count(*) FROM %s', rel) INTO v_nrows;

    -- Materialize the working set ONCE into a temp table, then run every
    -- per-column stat below against it. Previously v_src was a (TABLESAMPLE…)
    -- or (… LIMIT) SUBQUERY, so each of the ~5 stats per column re-ran it —
    -- re-scanning a multi-GB table (or re-executing a view) N_columns×5 times.
    -- One materialization makes the per-column passes hit a tiny local table;
    -- small tables read directly (already cheap).
    EXECUTE 'DROP TABLE IF EXISTS _fp_sample';
    IF v_nrows > sample_rows THEN
        v_sampled := true;
        IF v_relkind IN ('r', 'm') THEN
            v_pct := greatest(0.000001, least(100.0, 100.0 * sample_rows / NULLIF(v_nrows, 0)));
            EXECUTE format(
                'CREATE TEMP TABLE _fp_sample AS SELECT * FROM %s TABLESAMPLE SYSTEM (%s)',
                rel, v_pct);
        ELSE
            EXECUTE format(
                'CREATE TEMP TABLE _fp_sample AS SELECT * FROM %s LIMIT %s',
                rel, sample_rows);
        END IF;
        v_src := '_fp_sample';
    ELSE
        v_src := rel::text;
    END IF;

    EXECUTE format('SELECT count(*) FROM %s', v_src) INTO v_nsampled;

    FOR rc IN
        SELECT a.attnum,
               a.attname,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               (NOT a.attnotnull) AS nullable,
               pg_get_expr(ad.adbin, ad.adrelid) AS col_default,
               col_description(a.attrelid, a.attnum) AS col_comment,
               EXISTS (SELECT 1 FROM pg_constraint pc
                        WHERE pc.conrelid = a.attrelid AND pc.contype = 'p'
                          AND a.attnum = ANY (pc.conkey)) AS is_pk,
               (SELECT cn.nspname || '.' || cf.relname || '.' || af.attname
                  FROM pg_constraint fc
                  JOIN pg_class cf      ON cf.oid = fc.confrelid
                  JOIN pg_namespace cn  ON cn.oid = cf.relnamespace
                  JOIN pg_attribute af  ON af.attrelid = fc.confrelid
                       AND af.attnum = fc.confkey[array_position(fc.conkey, a.attnum)]
                 WHERE fc.conrelid = a.attrelid AND fc.contype = 'f'
                   AND a.attnum = ANY (fc.conkey)
                 LIMIT 1) AS fk_target
          FROM pg_attribute a
          LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
         WHERE a.attrelid = rel AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
    LOOP
        EXECUTE format('SELECT count(*), count(%I) FROM %s', rc.attname, v_src)
            INTO v_seen, v_nonnull;
        v_nulls := v_seen - v_nonnull;
        v_nullfrac := CASE WHEN v_seen > 0 THEN v_nulls::float8 / v_seen ELSE NULL END;

        BEGIN
            EXECUTE format('SELECT count(DISTINCT %I) FROM %s', rc.attname, v_src) INTO v_ndv;
        EXCEPTION WHEN others THEN v_ndv := NULL; END;

        BEGIN
            EXECUTE format('SELECT min(%I)::text, max(%I)::text FROM %s',
                           rc.attname, rc.attname, v_src) INTO v_min, v_max;
        EXCEPTION WHEN others THEN v_min := NULL; v_max := NULL; END;

        -- Value distribution: value -> count for up to distinct_cap values.
        -- Complete (every distinct value present) when ndv <= cap; powers exact
        -- new/lost value detection + PSI drift. example_values is derived from
        -- this so we scan once.
        v_dist := NULL;
        BEGIN
            EXECUTE format(
                $q$SELECT jsonb_object_agg(t.v, t.c)
                     FROM (SELECT %I::text AS v, count(*) AS c
                             FROM %s WHERE %I IS NOT NULL
                            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT %s) t$q$,
                rc.attname, v_src, rc.attname, distinct_cap) INTO v_dist;
        EXCEPTION WHEN others THEN v_dist := NULL; END;

        SELECT COALESCE(
                 jsonb_agg(jsonb_build_object('value', s.k, 'n', s.n) ORDER BY s.n DESC, s.k),
                 '[]'::jsonb)
          INTO v_examples
          FROM (SELECT d.key AS k, (d.value)::bigint AS n
                  FROM jsonb_each_text(COALESCE(v_dist, '{}'::jsonb)) AS d(key, value)
                 ORDER BY (d.value)::bigint DESC, d.key
                 LIMIT examples_k) s;

        -- Quantiles for numeric columns (non-numeric types raise -> NULL).
        v_quantiles := NULL;
        BEGIN
            EXECUTE format(
                $q$SELECT jsonb_build_object(
                      'p05', percentile_cont(0.05) WITHIN GROUP (ORDER BY %I),
                      'p25', percentile_cont(0.25) WITHIN GROUP (ORDER BY %I),
                      'p50', percentile_cont(0.50) WITHIN GROUP (ORDER BY %I),
                      'p75', percentile_cont(0.75) WITHIN GROUP (ORDER BY %I),
                      'p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY %I))
                     FROM %s WHERE %I IS NOT NULL$q$,
                rc.attname, rc.attname, rc.attname, rc.attname, rc.attname, v_src, rc.attname)
                INTO v_quantiles;
        EXCEPTION WHEN others THEN v_quantiles := NULL; END;

        v_columns := v_columns || jsonb_build_object(
            'name',          rc.attname,
            'ordinal',       rc.attnum,
            'data_type',     rc.data_type,
            'nullable',      rc.nullable,
            'default',       rc.col_default,
            'comment',       rc.col_comment,
            'is_pk',         rc.is_pk,
            'is_fk',         (rc.fk_target IS NOT NULL),
            'fk_target',     rc.fk_target,
            'n_seen',        v_seen,
            'n_nulls',       v_nulls,
            'null_frac',     v_nullfrac,
            'ndv',           v_ndv,
            'ndv_method',    CASE WHEN v_sampled THEN 'sampled' ELSE 'exact' END,
            'min',           v_min,
            'max',           v_max,
            'example_values', COALESCE(v_examples, '[]'::jsonb),
            'value_dist',     v_dist,
            'value_dist_complete', (v_ndv IS NOT NULL AND v_ndv <= distinct_cap),
            'quantiles',      v_quantiles);
    END LOOP;

    EXECUTE 'DROP TABLE IF EXISTS _fp_sample';

    RETURN jsonb_build_object(
        'rel',         rel::text,
        'oid',         (rel::oid)::text,
        'schema',      v_schema,
        'table',       v_table,
        'relkind',     v_relkind,
        'comment',     v_comment,
        'size_bytes',  v_size,
        'n_rows',      v_nrows,
        'n_sampled',   v_nsampled,
        'sampled',     v_sampled,
        'n_columns',   jsonb_array_length(v_columns),
        'columns',     v_columns,
        'profiled_at', now());
END $fn$;
