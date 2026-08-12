-- 0283: Calliope Playbooks -- reusable, identity-scoped methods learned from work.
--
-- A Playbook is not an execution graph.  Its immutable semantic versions retain
-- an outcome, applicability, method, guardrails, completion evidence, and
-- capability preferences.  Concrete tools remain a run-time decision.
--
-- Every Playbook has an explicit cap_playbook policy, so the compatibility rule
-- for older ungoverned capabilities can never accidentally make a new private
-- Playbook public.  Drafts remain owner-visible domain records; only an approved
-- active version receives a searchable capability projection.

ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS playbook_source_turn_id uuid
        REFERENCES rvbbit.calliope_turns(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_playbooks (
    id uuid PRIMARY KEY,
    capability_kind text GENERATED ALWAYS AS ('cap_playbook'::text) STORED,
    capability_name text NOT NULL UNIQUE,
    owner_email text NOT NULL,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    latest_version integer NOT NULL DEFAULT 1,
    approved_version integer,
    access_revision bigint NOT NULL DEFAULT 1,
    archived boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    approved_by text,
    archived_at timestamptz,
    archived_by text,
    CONSTRAINT calliope_playbooks_capability_name_check CHECK (
        capability_name ~ '^[a-z0-9][a-z0-9._~-]{0,199}$'
    ),
    CONSTRAINT calliope_playbooks_owner_check CHECK (
        owner_email=lower(btrim(owner_email)) AND owner_email LIKE '%@%'
    ),
    CONSTRAINT calliope_playbooks_version_check CHECK (
        latest_version >= 1 AND (
            approved_version IS NULL OR
            (approved_version >= 1 AND approved_version <= latest_version)
        )
    ),
    CONSTRAINT calliope_playbooks_access_revision_check CHECK (access_revision >= 1),
    CONSTRAINT calliope_playbooks_policy_fkey FOREIGN KEY (
        capability_kind,capability_name
    ) REFERENCES rvbbit.capability_access_policies(
        capability_kind,capability_name
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_playbook_versions (
    id uuid PRIMARY KEY,
    playbook_id uuid NOT NULL REFERENCES rvbbit.calliope_playbooks(id) ON DELETE CASCADE,
    version integer NOT NULL,
    title text NOT NULL,
    synopsis text NOT NULL,
    readiness text NOT NULL DEFAULT 'ready',
    semantic_contract jsonb NOT NULL,
    contract_hash text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    source_turn_id uuid REFERENCES rvbbit.calliope_turns(id) ON DELETE SET NULL,
    source_from_ordinal integer,
    source_through_ordinal integer,
    sketch_id uuid,
    sketch_revision integer,
    change_summary text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_playbook_versions_version_check CHECK (version >= 1),
    CONSTRAINT calliope_playbook_versions_title_check
        CHECK (length(btrim(title)) BETWEEN 1 AND 180),
    CONSTRAINT calliope_playbook_versions_synopsis_check
        CHECK (length(btrim(synopsis)) BETWEEN 1 AND 1200),
    CONSTRAINT calliope_playbook_versions_readiness_check
        CHECK (readiness IN ('ready','degraded','blocked')),
    CONSTRAINT calliope_playbook_versions_contract_check CHECK (
        jsonb_typeof(semantic_contract)='object'
        AND jsonb_typeof(semantic_contract->'when_to_use')='array'
        AND jsonb_typeof(semantic_contract->'triggers')='array'
        AND jsonb_typeof(semantic_contract->'when_not_to_use')='array'
        AND jsonb_typeof(semantic_contract->'context_to_gather')='array'
        AND jsonb_typeof(semantic_contract->'method')='array'
        AND jsonb_typeof(semantic_contract->'guardrails')='array'
        AND jsonb_typeof(semantic_contract->'completion_criteria')='array'
        AND jsonb_typeof(semantic_contract->'fallbacks')='array'
        AND jsonb_typeof(semantic_contract->'required_capabilities')='array'
        AND jsonb_typeof(semantic_contract->'preferred_capabilities')='array'
        AND jsonb_typeof(semantic_contract->'optional_capabilities')='array'
        AND length(btrim(semantic_contract->>'outcome')) > 0
        AND length(btrim(semantic_contract->>'deliverable')) > 0
    ),
    CONSTRAINT calliope_playbook_versions_evidence_check
        CHECK (jsonb_typeof(evidence_refs)='array'),
    CONSTRAINT calliope_playbook_versions_source_range_check CHECK (
        (source_from_ordinal IS NULL AND source_through_ordinal IS NULL)
        OR (
            source_from_ordinal >= 0
            AND source_through_ordinal >= source_from_ordinal
        )
    ),
    CONSTRAINT calliope_playbook_versions_sketch_revision_check
        CHECK (sketch_revision IS NULL OR sketch_revision >= 1),
    CONSTRAINT calliope_playbook_versions_hash_check
        CHECK (contract_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT calliope_playbook_versions_playbook_version_key
        UNIQUE (playbook_id,version)
);

-- This migration was exercised by Warehouse before its first packaged release.
-- Keep replay additive so those installations receive the evidence-pin columns
-- without replacing their already-created immutable version table.
ALTER TABLE rvbbit.calliope_playbook_versions
    ADD COLUMN IF NOT EXISTS source_turn_id uuid
        REFERENCES rvbbit.calliope_turns(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_from_ordinal integer,
    ADD COLUMN IF NOT EXISTS source_through_ordinal integer,
    ADD COLUMN IF NOT EXISTS sketch_revision integer;

DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='rvbbit.calliope_playbook_versions'::regclass
           AND conname='calliope_playbook_versions_source_range_check'
    ) THEN
        ALTER TABLE rvbbit.calliope_playbook_versions
            ADD CONSTRAINT calliope_playbook_versions_source_range_check CHECK (
                (source_from_ordinal IS NULL AND source_through_ordinal IS NULL)
                OR (
                    source_from_ordinal >= 0
                    AND source_through_ordinal >= source_from_ordinal
                )
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='rvbbit.calliope_playbook_versions'::regclass
           AND conname='calliope_playbook_versions_sketch_revision_check'
    ) THEN
        ALTER TABLE rvbbit.calliope_playbook_versions
            ADD CONSTRAINT calliope_playbook_versions_sketch_revision_check
            CHECK (sketch_revision IS NULL OR sketch_revision >= 1);
    END IF;
END
$ddl$;

CREATE INDEX IF NOT EXISTS calliope_playbooks_owner_updated_idx
    ON rvbbit.calliope_playbooks (owner_email,archived,updated_at DESC);
CREATE INDEX IF NOT EXISTS calliope_playbooks_approved_idx
    ON rvbbit.calliope_playbooks (updated_at DESC)
    WHERE approved_version IS NOT NULL AND NOT archived;
CREATE INDEX IF NOT EXISTS calliope_playbook_versions_playbook_idx
    ON rvbbit.calliope_playbook_versions (playbook_id,version DESC);

CREATE OR REPLACE FUNCTION rvbbit._calliope_playbook_versions_immutable()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'Calliope Playbook versions are immutable';
END
$fn$;
DROP TRIGGER IF EXISTS calliope_playbook_versions_immutable
    ON rvbbit.calliope_playbook_versions;
CREATE TRIGGER calliope_playbook_versions_immutable
BEFORE UPDATE OR DELETE ON rvbbit.calliope_playbook_versions
FOR EACH ROW EXECUTE FUNCTION rvbbit._calliope_playbook_versions_immutable();

CREATE OR REPLACE FUNCTION rvbbit.calliope_playbook_can_view(
    p_playbook_id uuid,
    p_subject text,
    p_include_archived boolean DEFAULT false
) RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
          FROM rvbbit.calliope_playbooks p
          JOIN rvbbit.capability_access_policies policy
            ON policy.capability_kind=p.capability_kind
           AND policy.capability_name=p.capability_name
         WHERE p.id=p_playbook_id
           AND (p_include_archived OR NOT p.archived)
           AND rvbbit.capability_can_use(
               p.capability_kind,p.capability_name,p_subject
           )
    )
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.calliope_playbook_capability_doc(
    p_playbook_id uuid
) RETURNS text
LANGUAGE sql STABLE
AS $fn$
    SELECT 'capability ' || p.capability_name
        || E'\nkind: cap_playbook'
        || E'\ntitle: ' || v.title
        || E'\nsignature: read_calliope_playbook(''' || p.capability_name || ''')'
        || E'\nversion: ' || v.version::text
        || E'\nreadiness: ' || v.readiness
        || E'\n' || v.synopsis
        || E'\noutcome: ' || (v.semantic_contract->>'outcome')
        || E'\nuse when: ' || coalesce((
            SELECT string_agg(value,'; ' ORDER BY ordinal)
              FROM jsonb_array_elements_text(v.semantic_contract->'when_to_use')
                   WITH ORDINALITY AS item(value,ordinal)
        ),'')
        || CASE WHEN jsonb_array_length(v.semantic_contract->'triggers') > 0
            THEN E'\ntrigger language: ' || coalesce((
                SELECT string_agg(value,', ' ORDER BY ordinal)
                  FROM jsonb_array_elements_text(v.semantic_contract->'triggers')
                       WITH ORDINALITY AS item(value,ordinal)
            ),'') ELSE '' END
        || CASE WHEN jsonb_array_length(v.semantic_contract->'required_capabilities') > 0
            THEN E'\nrequires: ' || coalesce((
                SELECT string_agg(value,', ' ORDER BY ordinal)
                  FROM jsonb_array_elements_text(v.semantic_contract->'required_capabilities')
                       WITH ORDINALITY AS item(value,ordinal)
            ),'') ELSE '' END
        || CASE WHEN jsonb_array_length(v.semantic_contract->'preferred_capabilities') > 0
            THEN E'\nprefers: ' || coalesce((
                SELECT string_agg(value,', ' ORDER BY ordinal)
                  FROM jsonb_array_elements_text(v.semantic_contract->'preferred_capabilities')
                       WITH ORDINALITY AS item(value,ordinal)
            ),'') ELSE '' END
        || E'\nAn identity-scoped reusable method. Load the authorized immutable version before applying it; concrete tools remain adaptive.'
      FROM rvbbit.calliope_playbooks p
      JOIN rvbbit.calliope_playbook_versions v
        ON v.playbook_id=p.id AND v.version=p.approved_version
     WHERE p.id=p_playbook_id
       AND p.approved_version IS NOT NULL
       AND NOT p.archived
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.sync_calliope_playbook_capability(
    p_playbook_id uuid,
    do_embed boolean DEFAULT true,
    embed_specialist text DEFAULT ''
) RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
    rec record;
    v_doc text;
    v_node bigint;
    v_embedding real[];
    v_existing_doc text;
BEGIN
    SELECT p.*,v.title,v.synopsis,v.readiness,v.semantic_contract,
           v.contract_hash,v.created_at AS version_created_at
      INTO rec
      FROM rvbbit.calliope_playbooks p
      LEFT JOIN rvbbit.calliope_playbook_versions v
        ON v.playbook_id=p.id AND v.version=p.approved_version
     WHERE p.id=p_playbook_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('status','missing','playbook_id',p_playbook_id);
    END IF;
    IF rec.archived OR rec.approved_version IS NULL THEN
        DELETE FROM rvbbit.catalog_docs
         WHERE graph_id='rvbbit_capabilities'
           AND kind='cap_playbook'
           AND rel_name=rec.capability_name;
        RETURN jsonb_build_object(
            'status',CASE WHEN rec.archived THEN 'archived' ELSE 'draft' END,
            'playbook_id',rec.id,
            'capability_name',rec.capability_name,
            'indexed',false
        );
    END IF;

    v_doc := rvbbit.calliope_playbook_capability_doc(rec.id);
    v_node := rvbbit.kg_assert_node(
        'cap_playbook',rec.capability_name,
        jsonb_strip_nulls(jsonb_build_object(
            'playbook_id',rec.id,
            'capability_name',rec.capability_name,
            'title',rec.title,
            'synopsis',rec.synopsis,
            'version',rec.approved_version,
            'readiness',rec.readiness,
            'required_capabilities',rec.semantic_contract->'required_capabilities',
            'preferred_capabilities',rec.semantic_contract->'preferred_capabilities',
            'contract_hash',rec.contract_hash,
            'search_doc',v_doc
        )),
        1.0,'',0.0,'rvbbit_capabilities'
    );

    SELECT d.doc,d.embedding INTO v_existing_doc,v_embedding
      FROM rvbbit.catalog_docs d
     WHERE d.graph_id='rvbbit_capabilities' AND d.node_id=v_node;
    IF v_existing_doc IS DISTINCT FROM v_doc THEN
        v_embedding := NULL;
    END IF;
    IF v_embedding IS NULL AND do_embed THEN
        BEGIN
            v_embedding := rvbbit.embed(v_doc,embed_specialist,'document');
        EXCEPTION WHEN others THEN
            v_embedding := NULL;
        END;
    END IF;

    INSERT INTO rvbbit.catalog_docs
        (node_id,graph_id,kind,schema_name,rel_name,col_name,doc,embedding,embedded_at,updated_at)
    VALUES (
        v_node,'rvbbit_capabilities','cap_playbook',NULL,rec.capability_name,NULL,
        v_doc,v_embedding,CASE WHEN v_embedding IS NOT NULL THEN now() END,now()
    )
    ON CONFLICT (graph_id,node_id) DO UPDATE SET
        kind=EXCLUDED.kind,
        schema_name=NULL,
        rel_name=EXCLUDED.rel_name,
        col_name=NULL,
        doc=EXCLUDED.doc,
        embedding=EXCLUDED.embedding,
        embedded_at=EXCLUDED.embedded_at,
        updated_at=now();

    RETURN jsonb_build_object(
        'status','approved',
        'playbook_id',rec.id,
        'capability_name',rec.capability_name,
        'version',rec.approved_version,
        'indexed',true,
        'embedded',v_embedding IS NOT NULL
    );
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.sync_calliope_playbook_capabilities(
    do_embed boolean DEFAULT true,
    embed_specialist text DEFAULT ''
) RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
    rec record;
    v_synced integer := 0;
    v_removed integer := 0;
BEGIN
    WITH removed AS (
        DELETE FROM rvbbit.catalog_docs d
         WHERE d.graph_id='rvbbit_capabilities'
           AND d.kind='cap_playbook'
           AND NOT EXISTS (
               SELECT 1 FROM rvbbit.calliope_playbooks p
                WHERE p.capability_name=d.rel_name
                  AND p.approved_version IS NOT NULL
                  AND NOT p.archived
           )
        RETURNING 1
    ) SELECT count(*)::int INTO v_removed FROM removed;

    FOR rec IN
        SELECT id FROM rvbbit.calliope_playbooks
         WHERE approved_version IS NOT NULL AND NOT archived
         ORDER BY updated_at,id
    LOOP
        PERFORM rvbbit.sync_calliope_playbook_capability(
            rec.id,do_embed,embed_specialist
        );
        v_synced := v_synced + 1;
    END LOOP;
    RETURN jsonb_build_object(
        'status','ok','synced',v_synced,'removed',v_removed
    );
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.capability_playbook_search_stale()
RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
          FROM rvbbit.calliope_playbooks p
          LEFT JOIN rvbbit.catalog_docs d
            ON d.graph_id='rvbbit_capabilities'
           AND d.kind='cap_playbook'
           AND d.rel_name=p.capability_name
         WHERE p.approved_version IS NOT NULL
           AND NOT p.archived
           AND (
               d.node_id IS NULL OR
               d.doc IS DISTINCT FROM rvbbit.calliope_playbook_capability_doc(p.id)
           )
    ) OR EXISTS (
        SELECT 1
          FROM rvbbit.catalog_docs d
         WHERE d.graph_id='rvbbit_capabilities'
           AND d.kind='cap_playbook'
           AND NOT EXISTS (
               SELECT 1 FROM rvbbit.calliope_playbooks p
                WHERE p.capability_name=d.rel_name
                  AND p.approved_version IS NOT NULL
                  AND NOT p.archived
           )
    )
$fn$;

COMMENT ON TABLE rvbbit.calliope_playbooks IS
    'Stable private-by-default Playbook identities and approved-version pointers; scheduling belongs to Assignments.';
COMMENT ON TABLE rvbbit.calliope_playbook_versions IS
    'Immutable semantic method contracts distilled from Calliope work; no executable graph or fixed tool sequence.';
COMMENT ON FUNCTION rvbbit.calliope_playbook_can_view(uuid,text,boolean) IS
    'Resolve a Playbook through its mandatory identity-scoped capability policy.';
COMMENT ON FUNCTION rvbbit.sync_calliope_playbook_capability(uuid,boolean,text) IS
    'Project one approved active Playbook into capability search without exposing private provenance.';
