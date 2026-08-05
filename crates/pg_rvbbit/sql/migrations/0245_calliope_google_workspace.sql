-- 0245: per-user Google Workspace grants and durable Sheets export receipts
--
-- Google sign-in proves identity but does not grant Workspace access.  This
-- separate, incremental grant is deliberately limited to drive.file: Calliope
-- may create and update only files it created (or a user explicitly opened for
-- it later).  Refresh tokens are encrypted by Warehouse before persistence.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_workspace_connections (
    owner_email text PRIMARY KEY,
    google_email text NOT NULL,
    refresh_token_ciphertext text NOT NULL,
    scopes text[] NOT NULL DEFAULT '{}'::text[],
    status text NOT NULL DEFAULT 'connected',
    connected_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    last_error text,
    CONSTRAINT calliope_google_workspace_connections_status_check
        CHECK (status IN ('connected','needs_reconnect','error'))
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_exports (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL
        REFERENCES rvbbit.calliope_google_workspace_connections(owner_email)
        ON DELETE CASCADE,
    session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    surface_id uuid REFERENCES rvbbit.calliope_surfaces(id) ON DELETE SET NULL,
    provider text NOT NULL DEFAULT 'google_sheets',
    provider_file_id text,
    title text NOT NULL,
    url text,
    sheet_name text NOT NULL DEFAULT 'Data',
    row_count integer NOT NULL DEFAULT 0,
    column_count integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'pending',
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT calliope_google_exports_provider_check
        CHECK (provider IN ('google_sheets')),
    CONSTRAINT calliope_google_exports_status_check
        CHECK (status IN ('pending','complete','failed')),
    CONSTRAINT calliope_google_exports_source_check
        CHECK (jsonb_typeof(source) = 'object')
);

CREATE INDEX IF NOT EXISTS calliope_google_exports_owner_created_idx
    ON rvbbit.calliope_google_exports (owner_email,created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_google_exports_surface_idx
    ON rvbbit.calliope_google_exports (surface_id,created_at DESC)
    WHERE surface_id IS NOT NULL;

COMMENT ON TABLE rvbbit.calliope_google_workspace_connections IS
    'Private per-user incremental Google Workspace grant; refresh_token_ciphertext is application-encrypted.';
COMMENT ON TABLE rvbbit.calliope_google_exports IS
    'Owner-scoped receipts for files exported by Calliope; source stores bounded provenance, never OAuth credentials.';
