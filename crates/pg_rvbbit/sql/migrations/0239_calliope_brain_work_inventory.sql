-- 0239: Brain-derived work inventory for Personal Briefs
--
-- Structured document sources already retain ticket / issue / pull-request
-- payloads in brain_documents.props.  This projection profiles those stable
-- source shapes and keeps a normalized, source-backed work index.  It does not
-- assign an item to a Calliope user: the service intersects this index with
-- brain_visible_docs(email) and resolves source identities at read time.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_brain_work_profiles (
    source_id bigint NOT NULL REFERENCES rvbbit.brain_sources(source_id) ON DELETE CASCADE,
    doc_type text NOT NULL DEFAULT 'document',
    shape_hash text NOT NULL,
    mapping_version integer NOT NULL DEFAULT 1,
    profile_source text NOT NULL DEFAULT 'inferred',
    status text NOT NULL DEFAULT 'possible',
    work_kind text NOT NULL DEFAULT 'work_item',
    confidence double precision NOT NULL DEFAULT 0,
    field_map jsonb NOT NULL DEFAULT '{}'::jsonb,
    lifecycle_map jsonb NOT NULL DEFAULT '{}'::jsonb,
    qualification jsonb NOT NULL DEFAULT '{}'::jsonb,
    sample_count integer NOT NULL DEFAULT 0,
    document_count bigint NOT NULL DEFAULT 0,
    last_source_sync_at timestamptz,
    last_document_ingested_at timestamptz,
    last_indexed_at timestamptz,
    profiled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    PRIMARY KEY (source_id,doc_type),
    CONSTRAINT calliope_brain_work_profiles_status_check
        CHECK (status IN ('active','possible','ignored','error')),
    CONSTRAINT calliope_brain_work_profiles_source_check
        CHECK (profile_source IN ('provider','source','provider+source','inferred')),
    CONSTRAINT calliope_brain_work_profiles_confidence_check
        CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT calliope_brain_work_profiles_counts_check
        CHECK (sample_count >= 0 AND document_count >= 0),
    CONSTRAINT calliope_brain_work_profiles_map_check
        CHECK (jsonb_typeof(field_map)='object'
           AND jsonb_typeof(lifecycle_map)='object'
           AND jsonb_typeof(qualification)='object')
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_brain_work_items (
    doc_id bigint PRIMARY KEY REFERENCES rvbbit.brain_documents(doc_id) ON DELETE CASCADE,
    source_id bigint NOT NULL REFERENCES rvbbit.brain_sources(source_id) ON DELETE CASCADE,
    profile_doc_type text NOT NULL DEFAULT 'document',
    profile_shape_hash text NOT NULL,
    work_kind text NOT NULL DEFAULT 'work_item',
    identifier text,
    title text NOT NULL,
    url text,
    status_label text,
    lifecycle text NOT NULL DEFAULT 'unknown',
    due_at timestamptz,
    priority_label text,
    source_updated_at timestamptz,
    relations jsonb NOT NULL DEFAULT '{}'::jsonb,
    project jsonb NOT NULL DEFAULT '{}'::jsonb,
    facts jsonb NOT NULL DEFAULT '{}'::jsonb,
    index_run_id uuid NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_brain_work_items_lifecycle_check
        CHECK (lifecycle IN ('open','in_progress','blocked','review','closed','canceled','unknown')),
    CONSTRAINT calliope_brain_work_items_json_check
        CHECK (jsonb_typeof(relations)='object'
           AND jsonb_typeof(project)='object'
           AND jsonb_typeof(facts)='object')
);

CREATE INDEX IF NOT EXISTS calliope_brain_work_items_source_lifecycle_idx
    ON rvbbit.calliope_brain_work_items (source_id,lifecycle,source_updated_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS calliope_brain_work_items_active_due_idx
    ON rvbbit.calliope_brain_work_items (due_at,source_id)
    WHERE lifecycle IN ('open','in_progress','blocked','review');
CREATE INDEX IF NOT EXISTS calliope_brain_work_items_relations_idx
    ON rvbbit.calliope_brain_work_items USING gin (relations jsonb_path_ops);

COMMENT ON TABLE rvbbit.calliope_brain_work_profiles IS
    'Deterministic source-shape profiles used to recognize structured work records in the document brain.';
COMMENT ON TABLE rvbbit.calliope_brain_work_items IS
    'Normalized source-backed work records. User visibility and assignment are resolved at Brief read time.';

-- Complete the built-in Linear shape without replacing any source-specific
-- overrides.  state.type is especially useful because display labels are
-- organization-specific while Linear lifecycle types are stable.
UPDATE rvbbit.brain_doc_providers
SET observation_map = jsonb_build_object(
    'status', jsonb_build_array('$.state.name', '$.status.name', '$.status'),
    'lifecycle', jsonb_build_array('$.state.type'),
    'due_at', jsonb_build_array('$.dueDate', '$.due_at', '$.dueAt'),
    'url', jsonb_build_array('$.url', '$.webUrl'),
    'identifier', jsonb_build_array('$.identifier', '$.id'),
    'updated_at', jsonb_build_array('$.updatedAt'),
    'priority', jsonb_build_array('$.priorityLabel', '$.priority'),
    'assignee_emails', jsonb_build_array('$.assignee.email'),
    'assignee_names', jsonb_build_array('$.assignee.name'),
    'assignee_ids', jsonb_build_array('$.assignee.id'),
    'participants', jsonb_build_array('$.subscribers[*].email'),
    'project', jsonb_build_array('$.project.name'),
    'team', jsonb_build_array('$.team.name'),
    'identity_directory', jsonb_build_object(
        'server', 'linear',
        'tool', 'linear_getUsers',
        'args', '{}'::jsonb,
        'email_paths', jsonb_build_array('$.email'),
        'aliases', jsonb_build_object(
            'external_id', jsonb_build_array('$.id'),
            'name', jsonb_build_array('$.name', '$.displayName')
        ),
        'ttl_seconds', 900
    )
) || observation_map, updated_at = now()
WHERE provider = 'linear-issues';
