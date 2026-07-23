-- 0208: backends generation — live spec-cache invalidation
--
-- The extension caches each backend's SpecialistSpec per connection
-- (SPEC_CACHE in specialists/mod.rs) and never invalidated it: change a
-- backend's endpoint_url and every warm pooled connection kept calling
-- the OLD host until it happened to reconnect. This bit the clover
-- endpoint migration: reinstall + UPDATE both "worked" (rows correct)
-- while live traffic from warm lens/MCP pool connections still hit the
-- old GPU box. "Restart after editing backends" is not a contract.
--
-- Fix: a generation sequence bumped by a statement trigger on ANY
-- rvbbit.backends write. The leader compares the sequence's current
-- value against the generation its cache was loaded at on every spec
-- load (sequence reads are non-transactional and cheap — one int off a
-- single page) and clears the whole spec cache when it moved. Pool
-- worker threads still never touch SPI; only the leader checks.

CREATE SEQUENCE IF NOT EXISTS rvbbit.backends_gen;

CREATE OR REPLACE FUNCTION rvbbit.backends_gen_bump() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM nextval('rvbbit.backends_gen');
    RETURN NULL;
END $$;

COMMENT ON FUNCTION rvbbit.backends_gen_bump() IS
    'Statement trigger on rvbbit.backends: bumps rvbbit.backends_gen so live connections drop their cached specialist specs (specialists/mod.rs checks the generation on every leader-side spec load).';

DROP TRIGGER IF EXISTS backends_gen_bump ON rvbbit.backends;
CREATE TRIGGER backends_gen_bump
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON rvbbit.backends
    FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.backends_gen_bump();

-- 0122 convention: rvbbit triggers survive session_replication_role=replica.
ALTER TABLE rvbbit.backends ENABLE ALWAYS TRIGGER backends_gen_bump;
