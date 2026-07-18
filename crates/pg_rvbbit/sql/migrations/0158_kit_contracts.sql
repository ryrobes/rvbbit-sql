-- 0158: Kit contracts — module gating for plates (KIT_PLATES_PLAN §7).
--
-- A contract is a named read-only query returning VIOLATION rows (empty =
-- green), in the spirit of operator tests and KPI checks. Plates may belong
-- to a module; a module is green when every one of its contracts returns no
-- rows. Red modules render only through the kit's switchboard — the gate is
-- enforced at render time, not just in the shelf UI.

ALTER TABLE rvbbit.plates ADD COLUMN IF NOT EXISTS module text;

CREATE TABLE IF NOT EXISTS rvbbit.kit_contracts (
    kit            text NOT NULL,
    module         text NOT NULL,
    contract_id    text NOT NULL,
    description    text,
    violations_sql text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (kit, module, contract_id)
);

CREATE OR REPLACE FUNCTION rvbbit.upsert_kit_contract(
    p_kit text,
    p_module text,
    p_contract_id text,
    p_violations_sql text,
    p_description text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $ukc$
BEGIN
    IF p_violations_sql !~* '^[[:space:]]*(SELECT|WITH)\y' THEN
        RAISE EXCEPTION 'contract % must be SELECT-shaped', p_contract_id;
    END IF;
    INSERT INTO rvbbit.kit_contracts (kit, module, contract_id, description, violations_sql)
    VALUES (p_kit, p_module, p_contract_id, p_description, p_violations_sql)
    ON CONFLICT (kit, module, contract_id) DO UPDATE SET
        description = EXCLUDED.description,
        violations_sql = EXCLUDED.violations_sql;
END
$ukc$;

-- Evaluate every contract for a kit. Errors in a contract's SQL count as a
-- violation (a broken gate must fail CLOSED, loudly).
CREATE OR REPLACE FUNCTION rvbbit.kit_contract_status(p_kit text)
RETURNS TABLE (
    module text,
    contract_id text,
    description text,
    ok boolean,
    violations bigint,
    sample text
)
LANGUAGE plpgsql
AS $kcs$
DECLARE
    c record;
    v_count bigint;
    v_sample text;
BEGIN
    FOR c IN
        SELECT k.module, k.contract_id, k.description, k.violations_sql
        FROM rvbbit.kit_contracts k
        WHERE k.kit = p_kit
        ORDER BY k.module, k.contract_id
    LOOP
        BEGIN
            EXECUTE format(
                'SELECT count(*), min(left(v.*::text, 200)) FROM (%s) v',
                c.violations_sql
            ) INTO v_count, v_sample;
        EXCEPTION WHEN OTHERS THEN
            v_count := 1;
            v_sample := 'contract error: ' || SQLERRM;
        END;
        module := c.module;
        contract_id := c.contract_id;
        description := c.description;
        ok := coalesce(v_count, 0) = 0;
        violations := coalesce(v_count, 0);
        sample := v_sample;
        RETURN NEXT;
    END LOOP;
END
$kcs$;
