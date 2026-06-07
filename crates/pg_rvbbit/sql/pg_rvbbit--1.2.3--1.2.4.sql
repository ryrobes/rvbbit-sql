-- pg_rvbbit 1.2.3 -> 1.2.4
--
-- FDW source substrate for the Postgres->rvbbit table-sync workflow: provision
-- a postgres_fdw connection to a source server and (re-)import foreign tables.
-- The read path is a plain non-locking SELECT on the source. fetch_size is set
-- on the server (default 100 rows/round-trip is a throughput killer for full
-- snapshot pulls).

CREATE OR REPLACE FUNCTION rvbbit.fdw_setup_server(
    server_name text,
    host text,
    port integer,
    dbname text,
    user_name text,
    password text,
    fetch_size integer DEFAULT 10000
) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS postgres_fdw;
    EXECUTE format(
        'CREATE SERVER IF NOT EXISTS %I FOREIGN DATA WRAPPER postgres_fdw '
        'OPTIONS (host %L, port %L, dbname %L, fetch_size %L)',
        server_name, host, port::text, dbname, fetch_size::text);
    EXECUTE format(
        'CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER SERVER %I '
        'OPTIONS (user %L, password %L)',
        server_name, user_name, password);
    RETURN format('postgres_fdw server "%s" ready (-> %s@%s:%s/%s)',
        server_name, user_name, host, port::text, dbname);
END;
$$;

-- (Re-)import foreign tables from a remote schema into a local schema. Drops
-- the targeted foreign tables first so a re-import picks up remote DDL
-- (added/changed columns) — that's how the sync tolerates source schema drift.
-- only_tables => NULL imports the whole schema; pass an array for à-la-carte.
-- Returns the number of foreign tables in the local schema afterward.
CREATE OR REPLACE FUNCTION rvbbit.fdw_import(
    server_name text,
    remote_schema text,
    local_schema text,
    only_tables text[] DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    t text;
    limit_clause text := '';
    n integer;
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', local_schema);
    IF only_tables IS NULL THEN
        FOR t IN
            SELECT c.relname FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = local_schema
        LOOP
            EXECUTE format('DROP FOREIGN TABLE IF EXISTS %I.%I', local_schema, t);
        END LOOP;
    ELSE
        FOREACH t IN ARRAY only_tables LOOP
            EXECUTE format('DROP FOREIGN TABLE IF EXISTS %I.%I', local_schema, t);
        END LOOP;
        limit_clause := format(' LIMIT TO (%s)',
            (SELECT string_agg(quote_ident(x), ', ') FROM unnest(only_tables) x));
    END IF;
    EXECUTE format('IMPORT FOREIGN SCHEMA %I%s FROM SERVER %I INTO %I',
        remote_schema, limit_clause, server_name, local_schema);
    SELECT count(*) INTO n FROM pg_foreign_table ft
        JOIN pg_class c ON c.oid = ft.ftrelid
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = local_schema;
    RETURN n;
END;
$$;
