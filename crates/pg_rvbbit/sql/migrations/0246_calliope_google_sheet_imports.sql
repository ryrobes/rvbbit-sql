-- 0246: owner-scoped Google Sheet snapshot receipts
--
-- Selection happens through Google Picker under the existing drive.file grant.
-- The receipt preserves only the explicitly selected workbook/tab/range and
-- immutable snapshot lineage; OAuth material never enters this table.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_imports (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL
        REFERENCES rvbbit.calliope_google_workspace_connections(owner_email)
        ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    surface_id uuid NOT NULL REFERENCES rvbbit.calliope_surfaces(id) ON DELETE CASCADE,
    provider text NOT NULL DEFAULT 'google_sheets',
    provider_file_id text NOT NULL,
    provider_sheet_id bigint NOT NULL,
    spreadsheet_title text NOT NULL,
    sheet_name text NOT NULL,
    selected_range text,
    first_row_header boolean NOT NULL DEFAULT true,
    row_count integer NOT NULL DEFAULT 0,
    column_count integer NOT NULL DEFAULT 0,
    snapshot_hash text NOT NULL,
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_google_imports_provider_check
        CHECK (provider IN ('google_sheets')),
    CONSTRAINT calliope_google_imports_source_check
        CHECK (jsonb_typeof(source) = 'object')
);

CREATE INDEX IF NOT EXISTS calliope_google_imports_owner_created_idx
    ON rvbbit.calliope_google_imports (owner_email,created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_google_imports_file_idx
    ON rvbbit.calliope_google_imports
       (owner_email,provider_file_id,provider_sheet_id,created_at DESC);

COMMENT ON TABLE rvbbit.calliope_google_imports IS
    'Private receipts and lineage for explicit Google Picker spreadsheet snapshots; contains no OAuth credentials.';
