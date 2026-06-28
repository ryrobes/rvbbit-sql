-- Upgrade pg_rvbbit 2.6.4 -> 2.7.0
--
-- Hindsight SQL wrappers are Rust pg_extern functions. Fresh installs get
-- these from generated full-extension SQL; upgraded databases need this edge.

CREATE OR REPLACE FUNCTION rvbbit.hindsight_recall(
    bank_id text,
    query text,
    options jsonb DEFAULT '{}'::jsonb,
    service_name text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hindsight_recall_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hindsight_reflect(
    bank_id text,
    query text,
    options jsonb DEFAULT '{}'::jsonb,
    service_name text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hindsight_reflect_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hindsight_retain(
    bank_id text,
    content text,
    options jsonb DEFAULT '{}'::jsonb,
    service_name text DEFAULT '',
    async_mode boolean DEFAULT true
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hindsight_retain_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hindsight_status(
    service_name text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hindsight_status_wrapper';
