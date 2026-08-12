-- 0290: keep mirror lineage pointed at the identifier dlt actually creates.
--
-- dlt's pinned snake_case normalizer turns names such as OrderLines into
-- order_lines before PostgreSQL materialization. New Calliope plans already
-- review and save that physical name. Tighten direct control-plane writes too,
-- while leaving any pre-release rows with older spellings visible for explicit
-- repair rather than guessing at a rename during migration.

ALTER TABLE rvbbit.mirror_tables
    DROP CONSTRAINT IF EXISTS mirror_tables_destination_check;

ALTER TABLE rvbbit.mirror_tables
    ADD CONSTRAINT mirror_tables_destination_check
    CHECK (destination_table ~ '^_?[a-z0-9]+(_[a-z0-9]+)*$')
    NOT VALID;

DO $validate_dlt_destination_identifiers$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM rvbbit.mirror_tables
        WHERE destination_table !~ '^_?[a-z0-9]+(_[a-z0-9]+)*$'
    ) THEN
        ALTER TABLE rvbbit.mirror_tables
            VALIDATE CONSTRAINT mirror_tables_destination_check;
    END IF;
END
$validate_dlt_destination_identifiers$;

COMMENT ON CONSTRAINT mirror_tables_destination_check
    ON rvbbit.mirror_tables IS
    'Destination names use dlt-normalized lowercase PostgreSQL identifiers so mirror_lineage resolves the physical relation.';
