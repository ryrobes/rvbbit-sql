-- =====================================================================
-- rvbbit 1.2.10 -> 1.2.11 : metric materialization (observations)
-- =====================================================================
-- Live reads stay live (re-run AS OF over the generations). Materialization is a
-- DURABLE, append-only log of what-was-reported: (value, verdict, threshold-
-- version, bitemporal coords, trigger). It outlives generation reaping and records
-- the KPI verdict AS-DECIDED. Default cadence = COMPACTION-TRIGGERED: a new
-- generation enqueues (table, gen, time); a drain (materialize_tick, pg_cron)
-- materializes the metrics that depend on that table, at def_as_of = the gen's
-- commit time (so the verdict is as it was believed then). Optional per-metric
-- cron overlay. Observations are immutable; "what it would have been under a new
-- def" stays a LIVE bitemporal query (check_metric over a past data_as_of).

-- ── observations: the durable append-only history ─────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.metric_observations (
    observation_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metric_name     text NOT NULL,
    metric_version  integer,
    def_as_of       timestamptz,
    data_as_of      timestamptz,
    data_generation bigint,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    value           jsonb,        -- full metric result (array of row objects)
    verdict         jsonb,        -- KPI verdict as-decided (nullable)
    status          text,         -- pass/fail/null (denormalized)
    observed_at     timestamptz NOT NULL DEFAULT now(),
    trigger         text NOT NULL DEFAULT 'manual'  -- compaction|cron|manual|backfill
);
CREATE INDEX IF NOT EXISTS metric_observations_name_data_idx
    ON rvbbit.metric_observations (metric_name, data_as_of DESC);
CREATE INDEX IF NOT EXISTS metric_observations_name_observed_idx
    ON rvbbit.metric_observations (metric_name, observed_at DESC);

-- ── per-metric materialization policy ─────────────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.metric_materialize (
    metric_name   text PRIMARY KEY,
    on_compaction boolean NOT NULL DEFAULT true,
    cron_schedule text,
    enabled       boolean NOT NULL DEFAULT true,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION rvbbit.set_materialize(
    p_name          text,
    p_on_compaction boolean DEFAULT true,
    p_cron_schedule text DEFAULT NULL,
    p_enabled       boolean DEFAULT true
) RETURNS void LANGUAGE sql AS $fn$
    INSERT INTO rvbbit.metric_materialize (metric_name, on_compaction, cron_schedule, enabled, updated_at)
    VALUES (p_name, p_on_compaction, p_cron_schedule, p_enabled, now())
    ON CONFLICT (metric_name) DO UPDATE
      SET on_compaction = EXCLUDED.on_compaction,
          cron_schedule = EXCLUDED.cron_schedule,
          enabled       = EXCLUDED.enabled,
          updated_at    = now();
$fn$;

-- ── metric -> table dependencies (route_explain-derived, cached) ──────
CREATE TABLE IF NOT EXISTS rvbbit.metric_dependencies (
    metric_name  text NOT NULL,
    table_oid    oid  NOT NULL,
    table_schema text,
    table_name   text,
    PRIMARY KEY (metric_name, table_oid)
);
CREATE INDEX IF NOT EXISTS metric_dependencies_table_idx
    ON rvbbit.metric_dependencies (table_oid);

-- Derive + cache which rvbbit tables a metric's latest def touches (route_explain
-- on the resolved SQL). Best-effort: never raises (tables may not exist yet).
CREATE OR REPLACE FUNCTION rvbbit.refresh_metric_dependencies(p_name text)
RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql   text;
    v_expl  jsonb;
    v_count integer := 0;
BEGIN
    BEGIN
        v_sql := rvbbit.metric_sql(p_name, '{}'::jsonb, now());
    EXCEPTION WHEN OTHERS THEN
        RETURN 0;
    END;
    IF v_sql IS NULL OR btrim(v_sql) = '' THEN RETURN 0; END IF;

    BEGIN
        v_expl := rvbbit.route_explain(v_sql);
    EXCEPTION WHEN OTHERS THEN
        RETURN 0;
    END;

    DELETE FROM rvbbit.metric_dependencies WHERE metric_name = p_name;
    INSERT INTO rvbbit.metric_dependencies (metric_name, table_oid, table_schema, table_name)
    SELECT p_name, (t->>'oid')::oid, t->>'schema', t->>'table'
    FROM jsonb_array_elements(coalesce(v_expl->'rvbbit_tables', '[]'::jsonb)) t
    WHERE (t->>'oid') IS NOT NULL
    ON CONFLICT (metric_name, table_oid) DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$fn$;

-- ── the core: run metric + check, append one observation ──────────────
CREATE OR REPLACE FUNCTION rvbbit.materialize_metric(
    p_name            text,
    p_params          jsonb DEFAULT '{}'::jsonb,
    p_def_as_of       timestamptz DEFAULT now(),
    p_data_as_of      timestamptz DEFAULT NULL,
    p_data_generation bigint DEFAULT NULL,
    p_trigger         text DEFAULT 'manual'
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
    v_value   jsonb;
    v_verdict jsonb;
    v_obs_id  bigint;
BEGIN
    SELECT version INTO v_version
    FROM rvbbit.metric_defs
    WHERE name = p_name AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC LIMIT 1;
    IF v_version IS NULL THEN
        RAISE EXCEPTION 'rvbbit.materialize_metric: metric "%" not defined as of %', p_name, p_def_as_of;
    END IF;

    SELECT jsonb_agg(obj) INTO v_value
    FROM rvbbit.metric(p_name, p_params, p_def_as_of, p_data_as_of) AS m(obj);

    v_verdict := rvbbit.check_metric(p_name, p_params, p_def_as_of, p_data_as_of);

    INSERT INTO rvbbit.metric_observations
        (metric_name, metric_version, def_as_of, data_as_of, data_generation,
         params, value, verdict, status, trigger)
    VALUES
        (p_name, v_version, p_def_as_of, coalesce(p_data_as_of, now()), p_data_generation,
         coalesce(p_params, '{}'::jsonb), v_value, v_verdict, v_verdict->>'status', p_trigger)
    RETURNING observation_id INTO v_obs_id;
    RETURN v_obs_id;
END;
$fn$;

-- ── compaction-triggered queue ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.materialize_queue (
    table_oid    oid NOT NULL,
    generation   bigint NOT NULL,
    committed_at timestamptz NOT NULL,
    enqueued_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, generation)
);

-- A new generation enqueues itself IF some metric depends on the table. Cheap and
-- exception-safe — materialization must never abort a compaction.
CREATE OR REPLACE FUNCTION rvbbit._enqueue_materialize() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    BEGIN
        IF EXISTS (SELECT 1 FROM rvbbit.metric_dependencies WHERE table_oid = NEW.table_oid) THEN
            INSERT INTO rvbbit.materialize_queue (table_oid, generation, committed_at)
            VALUES (NEW.table_oid, NEW.generation, NEW.committed_at)
            ON CONFLICT (table_oid, generation) DO NOTHING;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        NULL;
    END;
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS rvbbit_generations_materialize ON rvbbit.generations;
CREATE TRIGGER rvbbit_generations_materialize
    AFTER INSERT ON rvbbit.generations
    FOR EACH ROW EXECUTE FUNCTION rvbbit._enqueue_materialize();

-- Drain the queue: per generation, materialize each dependent + enabled metric at
-- def_as_of = the generation's commit time (verdict as-it-was). pg_cron heartbeat.
CREATE OR REPLACE FUNCTION rvbbit.materialize_tick(p_max integer DEFAULT 200)
RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_item   record;
    v_metric text;
    v_done   integer := 0;
BEGIN
    FOR v_item IN
        SELECT table_oid, generation, committed_at
        FROM rvbbit.materialize_queue
        ORDER BY enqueued_at
        LIMIT greatest(p_max, 1)
        FOR UPDATE SKIP LOCKED
    LOOP
        FOR v_metric IN
            SELECT d.metric_name
            FROM rvbbit.metric_dependencies d
            JOIN rvbbit.metric_materialize p ON p.metric_name = d.metric_name
            WHERE d.table_oid = v_item.table_oid
              AND p.enabled AND p.on_compaction
        LOOP
            BEGIN
                PERFORM rvbbit.materialize_metric(
                    v_metric, '{}'::jsonb, v_item.committed_at, v_item.committed_at,
                    v_item.generation, 'compaction');
                v_done := v_done + 1;
            EXCEPTION WHEN OTHERS THEN
                NULL;  -- best-effort per metric
            END;
        END LOOP;
        DELETE FROM rvbbit.materialize_queue
        WHERE table_oid = v_item.table_oid AND generation = v_item.generation;
    END LOOP;
    RETURN v_done;
END;
$fn$;

-- Convenience reader: recent observations for a metric (newest first).
CREATE OR REPLACE FUNCTION rvbbit.metric_history(p_name text, p_limit integer DEFAULT 200)
RETURNS TABLE(observation_id bigint, metric_version integer, def_as_of timestamptz,
              data_as_of timestamptz, data_generation bigint, value jsonb,
              verdict jsonb, status text, observed_at timestamptz, trigger text)
LANGUAGE sql STABLE AS $fn$
    SELECT observation_id, metric_version, def_as_of, data_as_of, data_generation,
           value, verdict, status, observed_at, trigger
    FROM rvbbit.metric_observations
    WHERE metric_name = p_name
    ORDER BY data_as_of DESC, observed_at DESC
    LIMIT greatest(p_limit, 1);
$fn$;

-- pg_cron registration (mirrors rvbbit.schedule_accel_tick).
CREATE OR REPLACE FUNCTION rvbbit.schedule_materialize_tick(
    cron_schedule text DEFAULT '* * * * *',
    budget        integer DEFAULT 200
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    jobid     bigint;
    cron_home text := current_setting('cron.database_name', true);
    this_db   text := current_database();
    command   text := format('SELECT rvbbit.materialize_tick(%s)', budget);
BEGIN
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format('connect to %L and run: SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home, 'rvbbit_materialize_tick', cron_schedule, command, this_db);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; call rvbbit.materialize_tick() manually.';
    END IF;
    EXECUTE format('SELECT cron.schedule(%L, %L, %L)', 'rvbbit_materialize_tick', cron_schedule, command)
        INTO jobid;
    RETURN jobid;
END;
$fn$;

-- define_metric now derives deps + defaults a metric to compaction-materialized.
CREATE OR REPLACE FUNCTION rvbbit.define_metric(
    p_name        text,
    p_sql         text,
    p_params      jsonb DEFAULT '{}'::jsonb,
    p_grain       text  DEFAULT NULL,
    p_description text  DEFAULT NULL,
    p_owner       text  DEFAULT NULL,
    p_labels      jsonb DEFAULT '{}'::jsonb,
    p_check       text  DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
BEGIN
    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_metric: name is required';
    END IF;
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_metric: sql is required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.metric:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version
    FROM rvbbit.metric_defs WHERE name = p_name;
    INSERT INTO rvbbit.metric_defs
        (name, version, sql, params, grain, description, owner, labels, check_sql)
    VALUES
        (p_name, v_version, p_sql, coalesce(p_params, '{}'::jsonb), p_grain,
         p_description, p_owner, coalesce(p_labels, '{}'::jsonb),
         CASE WHEN btrim(coalesce(p_check, '')) = '' THEN NULL ELSE p_check END);
    -- best-effort: cache table deps + default to compaction-materialized.
    PERFORM rvbbit.refresh_metric_dependencies(p_name);
    INSERT INTO rvbbit.metric_materialize (metric_name) VALUES (p_name)
        ON CONFLICT (metric_name) DO NOTHING;
    RETURN v_version;
END;
$fn$;
