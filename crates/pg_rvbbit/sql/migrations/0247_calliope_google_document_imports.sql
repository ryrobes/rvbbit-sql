-- 0247: owner-private Google Docs imported through Calliope Picker
--
-- The Google document body lives in the ordinary Brain corpus so it receives
-- normal chunking, semantic retrieval, and later enrichment.  Visibility is
-- granted through a deterministic per-owner Brain role; the receipt keeps the
-- authenticated owner and source revision explicit without storing OAuth data.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_document_imports (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL
        REFERENCES rvbbit.calliope_google_workspace_connections(owner_email)
        ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    surface_id uuid NOT NULL REFERENCES rvbbit.calliope_surfaces(id) ON DELETE CASCADE,
    brain_doc_id bigint NOT NULL REFERENCES rvbbit.brain_documents(doc_id) ON DELETE CASCADE,
    private_role text NOT NULL,
    provider text NOT NULL DEFAULT 'google_docs',
    provider_file_id text NOT NULL,
    document_title text NOT NULL,
    revision_id text,
    character_count integer NOT NULL DEFAULT 0,
    word_count integer NOT NULL DEFAULT 0,
    tab_count integer NOT NULL DEFAULT 1,
    content_hash text NOT NULL,
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_google_document_imports_provider_check
        CHECK (provider IN ('google_docs')),
    CONSTRAINT calliope_google_document_imports_counts_check
        CHECK (character_count >= 0 AND word_count >= 0 AND tab_count >= 1),
    CONSTRAINT calliope_google_document_imports_source_check
        CHECK (jsonb_typeof(source) = 'object')
);

CREATE INDEX IF NOT EXISTS calliope_google_document_imports_owner_created_idx
    ON rvbbit.calliope_google_document_imports (owner_email,created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_google_document_imports_file_idx
    ON rvbbit.calliope_google_document_imports
       (owner_email,provider_file_id,created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_google_document_imports_brain_doc_idx
    ON rvbbit.calliope_google_document_imports (brain_doc_id,created_at DESC);

COMMENT ON TABLE rvbbit.calliope_google_document_imports IS
    'Owner-scoped receipts linking explicitly Picker-selected Google Docs to private Brain documents; contains no OAuth credentials.';
