-- 0136_fleet_endpoints.sql
-- The fleet registry: named engine endpoints the brain may offload to. This
-- replaces the hand-set rvbbit.duck_fleet_endpoint GUC as the source of truth
-- (the GUC survives as a session-level override/pin — same pattern as
-- route_force_candidate). Warren-fed enrollment lands later; for now rows are
-- managed by fleet_add/fleet_remove and health comes from fleet_probe().

CREATE TABLE IF NOT EXISTS rvbbit.fleet_endpoints (
    name             text PRIMARY KEY,
    endpoint         text NOT NULL,              -- host:port of rvbbit-duck --serve-tcp
    engine           text NOT NULL DEFAULT 'duck',
    enabled          boolean NOT NULL DEFAULT true,
    notes            text,
    added_at         timestamptz NOT NULL DEFAULT now(),
    last_probe_at    timestamptz,
    last_probe_ms    double precision,
    last_probe_ok    boolean,
    last_probe_error text
);

CREATE OR REPLACE FUNCTION rvbbit.fleet_add(
    node_name text, node_endpoint text, node_engine text DEFAULT 'duck', node_notes text DEFAULT NULL
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO rvbbit.fleet_endpoints (name, endpoint, engine, notes)
    VALUES (node_name, node_endpoint, node_engine, node_notes)
    ON CONFLICT (name) DO UPDATE
      SET endpoint = EXCLUDED.endpoint, engine = EXCLUDED.engine,
          notes = coalesce(EXCLUDED.notes, rvbbit.fleet_endpoints.notes),
          enabled = true;
$$;

CREATE OR REPLACE FUNCTION rvbbit.fleet_remove(node_name text)
RETURNS boolean LANGUAGE sql AS $$
    DELETE FROM rvbbit.fleet_endpoints WHERE name = node_name RETURNING true;
$$;

CREATE OR REPLACE FUNCTION rvbbit.fleet_set_enabled(node_name text, node_enabled boolean)
RETURNS void LANGUAGE sql AS $$
    UPDATE rvbbit.fleet_endpoints SET enabled = node_enabled WHERE name = node_name;
$$;

-- C binding: send "SELECT 1" through the fleet transport to one endpoint and
-- record the outcome on its row. Returns the probe report.
CREATE OR REPLACE FUNCTION rvbbit.fleet_probe(node_name text)
RETURNS jsonb LANGUAGE c STRICT AS '$libdir/pg_rvbbit', 'fleet_probe_wrapper';

-- Probe every enabled endpoint; one row per node.
CREATE OR REPLACE FUNCTION rvbbit.fleet_doctor()
RETURNS SETOF jsonb LANGUAGE sql AS $$
    SELECT rvbbit.fleet_probe(name) FROM rvbbit.fleet_endpoints WHERE enabled ORDER BY name;
$$;

-- The at-a-glance view the Fleet UI reads.
CREATE OR REPLACE VIEW rvbbit.fleet AS
SELECT name, endpoint, engine, enabled,
       last_probe_ok, last_probe_ms, last_probe_at, last_probe_error,
       added_at, notes
FROM rvbbit.fleet_endpoints
ORDER BY enabled DESC, name;
