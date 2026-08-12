-- 0293: follow-through from the first full hosted onboarding run.
--
-- A company can mirror several source schemas into one concise local schema.
-- Job identity and lineage stay source-shaped; only an actual destination
-- relation collision is rejected. The first useful business question is asked
-- against mirrored data at the end of setup, not required in the company form.

ALTER TABLE rvbbit.company_profile
    DROP CONSTRAINT IF EXISTS company_profile_questions_check;
ALTER TABLE rvbbit.company_profile
    ADD CONSTRAINT company_profile_questions_check
    CHECK (cardinality(business_questions) BETWEEN 0 AND 20);

ALTER TABLE rvbbit.mirror_jobs
    DROP CONSTRAINT IF EXISTS mirror_jobs_destination_schema_key;

CREATE OR REPLACE FUNCTION rvbbit.assert_mirror_destination_relation_available(
    requested_job_name text,
    requested_destination_table text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER STABLE
SET search_path = pg_catalog, rvbbit
AS $assert_mirror_destination_relation_available$
DECLARE
    requested_destination_schema text;
    conflicting_job text;
BEGIN
    SELECT destination_schema
      INTO requested_destination_schema
      FROM rvbbit.mirror_jobs
     WHERE job_name = requested_job_name;
    IF requested_destination_schema IS NULL THEN
        RETURN;
    END IF;

    SELECT existing.job_name
      INTO conflicting_job
      FROM rvbbit.mirror_tables existing
      JOIN rvbbit.mirror_jobs existing_job
        ON existing_job.job_name = existing.job_name
     WHERE existing.job_name <> requested_job_name
       AND existing_job.destination_schema = requested_destination_schema
       AND existing.destination_table = requested_destination_table
     LIMIT 1;
    IF conflicting_job IS NOT NULL THEN
        RAISE EXCEPTION
            'destination relation %.% is already owned by mirror job %',
            requested_destination_schema, requested_destination_table, conflicting_job
            USING ERRCODE = '23505',
                  CONSTRAINT = 'mirror_tables_destination_relation_key',
                  HINT = 'Choose a distinct local table name or add the source table to the existing job.';
    END IF;
END
$assert_mirror_destination_relation_available$;

CREATE OR REPLACE FUNCTION rvbbit.enforce_mirror_table_destination_relation()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $enforce_mirror_table_destination_relation$
BEGIN
    PERFORM rvbbit.assert_mirror_destination_relation_available(
        NEW.job_name, NEW.destination_table
    );
    RETURN NEW;
END
$enforce_mirror_table_destination_relation$;

DROP TRIGGER IF EXISTS mirror_table_destination_relation_guard
    ON rvbbit.mirror_tables;
CREATE TRIGGER mirror_table_destination_relation_guard
BEFORE INSERT OR UPDATE ON rvbbit.mirror_tables
FOR EACH ROW EXECUTE FUNCTION rvbbit.enforce_mirror_table_destination_relation();

CREATE OR REPLACE FUNCTION rvbbit.enforce_mirror_job_destination_schema()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $enforce_mirror_job_destination_schema$
DECLARE
    collision record;
BEGIN
    IF NEW.destination_schema IS NOT DISTINCT FROM OLD.destination_schema THEN
        RETURN NEW;
    END IF;
    SELECT own.destination_table, other.job_name
      INTO collision
      FROM rvbbit.mirror_tables own
      JOIN rvbbit.mirror_tables other
        ON other.destination_table = own.destination_table
       AND other.job_name <> own.job_name
      JOIN rvbbit.mirror_jobs other_job
        ON other_job.job_name = other.job_name
     WHERE own.job_name = NEW.job_name
       AND other_job.destination_schema = NEW.destination_schema
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'destination relation %.% is already owned by mirror job %',
            NEW.destination_schema, collision.destination_table, collision.job_name
            USING ERRCODE = '23505',
                  CONSTRAINT = 'mirror_tables_destination_relation_key',
                  HINT = 'Choose a distinct local table name before moving this mirror job.';
    END IF;
    RETURN NEW;
END
$enforce_mirror_job_destination_schema$;

DROP TRIGGER IF EXISTS mirror_job_destination_schema_guard
    ON rvbbit.mirror_jobs;
CREATE TRIGGER mirror_job_destination_schema_guard
BEFORE UPDATE OF destination_schema ON rvbbit.mirror_jobs
FOR EACH ROW EXECUTE FUNCTION rvbbit.enforce_mirror_job_destination_schema();

REVOKE ALL ON FUNCTION rvbbit.assert_mirror_destination_relation_available(text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.enforce_mirror_table_destination_relation()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.enforce_mirror_job_destination_schema()
    FROM PUBLIC;
