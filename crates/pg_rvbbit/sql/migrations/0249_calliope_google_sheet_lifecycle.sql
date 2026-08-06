-- 0249: lifecycle for owner-private Google Sheet snapshots
--
-- Every refresh remains an immutable Stage surface. One receipt per exact
-- owner/workbook/tab/range/header interpretation is current; a changed refresh
-- supersedes it, while an unchanged check only advances last_checked_at.

ALTER TABLE rvbbit.calliope_google_imports
    ADD COLUMN IF NOT EXISTS operation text NOT NULL DEFAULT 'import',
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS last_checked_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS superseded_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error text;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY lower(owner_email),provider_file_id,provider_sheet_id,
                            coalesce(selected_range,''),first_row_header
               ORDER BY created_at DESC,id DESC
           ) AS position
      FROM rvbbit.calliope_google_imports
     WHERE status = 'active'
)
UPDATE rvbbit.calliope_google_imports i
   SET status = 'superseded',
       superseded_at = coalesce(i.superseded_at,now())
  FROM ranked r
 WHERE i.id = r.id
   AND r.position > 1;

DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'rvbbit.calliope_google_imports'::regclass
           AND conname = 'calliope_google_imports_operation_check'
    ) THEN
        ALTER TABLE rvbbit.calliope_google_imports
            ADD CONSTRAINT calliope_google_imports_operation_check
            CHECK (operation IN ('import','refresh'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'rvbbit.calliope_google_imports'::regclass
           AND conname = 'calliope_google_imports_status_check'
    ) THEN
        ALTER TABLE rvbbit.calliope_google_imports
            ADD CONSTRAINT calliope_google_imports_status_check
            CHECK (status IN ('active','superseded'));
    END IF;
END
$ddl$;

CREATE UNIQUE INDEX IF NOT EXISTS calliope_google_imports_active_source_uidx
    ON rvbbit.calliope_google_imports (
        lower(owner_email),provider_file_id,provider_sheet_id,
        coalesce(selected_range,''),first_row_header
    )
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS calliope_google_imports_status_idx
    ON rvbbit.calliope_google_imports (owner_email,status,created_at DESC);

COMMENT ON COLUMN rvbbit.calliope_google_imports.status IS
    'Current lifecycle of this immutable Sheet snapshot receipt: active or superseded by a changed refresh.';
