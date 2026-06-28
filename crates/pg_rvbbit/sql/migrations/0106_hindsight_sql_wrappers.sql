-- 0106_hindsight_sql_wrappers
--
-- rvbbit.migrate() is the operational upgrade path for most deployments.
-- Rust pg_extern functions are normally created by CREATE/ALTER EXTENSION, so
-- databases that only run migrate() after a redeploy need an idempotent repair
-- migration for the Hindsight SQL wrappers.

CREATE OR REPLACE FUNCTION rvbbit.hindsight_recall(
    bank_id text,
    query text,
    options jsonb DEFAULT '{}'::jsonb,
    service_name text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'hindsight_recall_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hindsight_reflect(
    bank_id text,
    query text,
    options jsonb DEFAULT '{}'::jsonb,
    service_name text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'hindsight_reflect_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hindsight_retain(
    bank_id text,
    content text,
    options jsonb DEFAULT '{}'::jsonb,
    service_name text DEFAULT '',
    async_mode boolean DEFAULT true
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'hindsight_retain_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hindsight_status(
    service_name text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'hindsight_status_wrapper';
