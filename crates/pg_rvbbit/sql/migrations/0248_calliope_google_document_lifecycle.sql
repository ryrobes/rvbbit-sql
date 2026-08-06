-- 0248: lifecycle for owner-private Google Docs
--
-- Imports remain immutable receipts, while one receipt per owner/file is the
-- active Brain copy.  A refresh supersedes the earlier receipt; forgetting a
-- document tombstones its Brain content without touching the Google original.

ALTER TABLE rvbbit.calliope_google_document_imports
    ADD COLUMN IF NOT EXISTS operation text NOT NULL DEFAULT 'import',
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS last_checked_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS superseded_at timestamptz,
    ADD COLUMN IF NOT EXISTS removed_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error text;

-- A pre-release installation may have imported the same file more than once.
-- Preserve every receipt, but make only the newest one current before adding
-- the partial uniqueness guard.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY lower(owner_email),provider_file_id
               ORDER BY created_at DESC,id DESC
           ) AS position
      FROM rvbbit.calliope_google_document_imports
     WHERE status = 'active'
)
UPDATE rvbbit.calliope_google_document_imports i
   SET status = 'superseded',
       superseded_at = coalesce(i.superseded_at,now())
  FROM ranked r
 WHERE i.id = r.id
   AND r.position > 1;

DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'rvbbit.calliope_google_document_imports'::regclass
           AND conname = 'calliope_google_document_imports_operation_check'
    ) THEN
        ALTER TABLE rvbbit.calliope_google_document_imports
            ADD CONSTRAINT calliope_google_document_imports_operation_check
            CHECK (operation IN ('import','refresh'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'rvbbit.calliope_google_document_imports'::regclass
           AND conname = 'calliope_google_document_imports_status_check'
    ) THEN
        ALTER TABLE rvbbit.calliope_google_document_imports
            ADD CONSTRAINT calliope_google_document_imports_status_check
            CHECK (status IN ('active','superseded','removed'));
    END IF;
END
$ddl$;

CREATE UNIQUE INDEX IF NOT EXISTS calliope_google_document_imports_active_file_uidx
    ON rvbbit.calliope_google_document_imports (lower(owner_email),provider_file_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS calliope_google_document_imports_status_idx
    ON rvbbit.calliope_google_document_imports (owner_email,status,created_at DESC);

COMMENT ON COLUMN rvbbit.calliope_google_document_imports.status IS
    'Current lifecycle of this immutable import receipt: active, superseded by a refresh, or removed from the private Brain.';
