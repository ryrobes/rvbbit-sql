-- Bootstrap + run the stacked SQL migrations. Idempotent; safe to re-run.
--
-- This is how schema changes reach a database — independent of the extension
-- version / ALTER EXTENSION UPDATE. `rvbbit.migrate()` is a C function in the
-- extension's .so; on an install that predates it the SQL binding won't exist
-- yet, so we (re)create the binding here (pointing at the loaded library, pgrx's
-- stable `<fn>_wrapper` symbol) and then run it. On a fresh install the binding
-- already exists and CREATE OR REPLACE is a harmless no-op.
--
-- Deploy step (Makefile reload-extension / docker init / prod one-shot):
--     psql -d <db> -f crates/pg_rvbbit/sql/migrate.sql
-- or simply `SELECT rvbbit.migrate();` once the binding exists.

CREATE OR REPLACE FUNCTION rvbbit.migrate()
    RETURNS text
    LANGUAGE c
    AS '$libdir/pg_rvbbit', 'migrate_wrapper';

SELECT rvbbit.migrate();
