-- 0279: Business Topology foundation.
--
-- The catalog KG describes database structure and the data KG stores concrete
-- claims.  Business Topology is the deliberately smaller layer between them:
-- inferred business concepts, their facets, where those facets manifest, and
-- the evidence behind each promoted relationship.
--
-- This migration establishes four contracts before any model is allowed to
-- make semantic claims:
--   1. A "population" is the inference atom.  It may be one field, a composite
--      field bundle, a row slice, an event stream, or a document mention set.
--      A relation is useful context/provenance, never assumed to be one object.
--   2. Profiling is bounded and privacy-first.  Raw values are never persisted;
--      salted value fingerprints remain in this database and model packets
--      contain only names, types, distributions, shapes, and context.
--   3. Every profile, inference, proposal, and promotion retains its exact
--      packet/model version.  "Similar", "joinable", "same concept", and
--      "same real-world instance" remain different verdicts.
--   4. Accepted topology is human-governed.  Models enqueue proposals; review
--      materializes nodes, population bindings, and typed edges.
--
-- The first specialist contracts are intentionally model-agnostic:
--   rvbbit.business-topology.population.v1
--   rvbbit.business-topology.correspondence.v1
-- Hutch/Clover workers can consume business_topology_inference_jobs in batches
-- and replace checkpoints without changing any stored business object.

INSERT INTO rvbbit.settings (key, value, updated_at)
VALUES (
    'business_topology_profile_salt',
    to_jsonb(gen_random_uuid()::text),
    now()
)
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_sources (
    source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key text NOT NULL UNIQUE,
    source_kind text NOT NULL DEFAULT 'postgres_relation',
    locator jsonb NOT NULL,
    relation_oid oid,
    schema_name text,
    relation_name text,
    relation_kind text,
    status text NOT NULL DEFAULT 'active',
    source_fingerprint text,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_profiled_at timestamptz,
    retired_at timestamptz,
    CONSTRAINT business_topology_sources_kind_check CHECK (
        source_kind IN (
            'postgres_relation','query_projection','document_collection',
            'event_stream','external_object'
        )
    ),
    CONSTRAINT business_topology_sources_status_check CHECK (
        status IN ('active','stale','retired')
    ),
    CONSTRAINT business_topology_sources_locator_check CHECK (
        jsonb_typeof(locator)='object'
    ),
    CONSTRAINT business_topology_sources_properties_check CHECK (
        jsonb_typeof(properties)='object'
    )
);
CREATE INDEX IF NOT EXISTS business_topology_sources_relation_idx
    ON rvbbit.business_topology_sources (relation_oid)
    WHERE relation_oid IS NOT NULL;
CREATE INDEX IF NOT EXISTS business_topology_sources_status_idx
    ON rvbbit.business_topology_sources (status,last_profiled_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_populations (
    population_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_sources(source_id) ON DELETE CASCADE,
    population_key text NOT NULL UNIQUE,
    population_kind text NOT NULL,
    selector jsonb NOT NULL,
    display_name text,
    status text NOT NULL DEFAULT 'active',
    current_profile_id bigint,
    created_by text NOT NULL DEFAULT current_user,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_profiled_at timestamptz,
    CONSTRAINT business_topology_populations_kind_check CHECK (
        population_kind IN (
            'field','record_context','composite','slice','event_stream',
            'mention_set','query_projection'
        )
    ),
    CONSTRAINT business_topology_populations_status_check CHECK (
        status IN ('active','stale','retired')
    ),
    CONSTRAINT business_topology_populations_selector_check CHECK (
        jsonb_typeof(selector)='object'
    )
);
CREATE INDEX IF NOT EXISTS business_topology_populations_source_idx
    ON rvbbit.business_topology_populations (source_id,status,population_kind);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_profile_snapshots (
    profile_id bigserial PRIMARY KEY,
    population_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE CASCADE,
    packet_version text NOT NULL,
    profile_hash text NOT NULL,
    model_input_hash text NOT NULL,
    source_fingerprint text NOT NULL,
    sample_rows integer NOT NULL DEFAULT 0,
    sampling_method text NOT NULL,
    profile jsonb NOT NULL,
    model_packet jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT business_topology_profile_snapshots_sample_check CHECK (
        sample_rows >= 0
    ),
    CONSTRAINT business_topology_profile_snapshots_json_check CHECK (
        jsonb_typeof(profile)='object' AND jsonb_typeof(model_packet)='object'
    ),
    CONSTRAINT business_topology_profile_snapshots_population_hash_key
        UNIQUE (population_id,profile_hash)
);
CREATE INDEX IF NOT EXISTS business_topology_profiles_population_time_idx
    ON rvbbit.business_topology_profile_snapshots
       (population_id,created_at DESC);
CREATE INDEX IF NOT EXISTS business_topology_profiles_model_input_idx
    ON rvbbit.business_topology_profile_snapshots (model_input_hash);

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname='business_topology_populations_current_profile_fkey'
           AND conrelid='rvbbit.business_topology_populations'::regclass
    ) THEN
        ALTER TABLE rvbbit.business_topology_populations
            ADD CONSTRAINT business_topology_populations_current_profile_fkey
            FOREIGN KEY (current_profile_id)
            REFERENCES rvbbit.business_topology_profile_snapshots(profile_id)
            ON DELETE SET NULL;
    END IF;
END
$migration$;

-- Fingerprints are intentionally separated from the model packet.  The salt
-- is installation-local, values are normalized before hashing, and old sets
-- disappear with their immutable profile snapshot.  This supports overlap and
-- containment evidence without retaining sampled emails, IDs, names, or codes.
CREATE TABLE IF NOT EXISTS rvbbit.business_topology_value_fingerprints (
    profile_id bigint NOT NULL
        REFERENCES rvbbit.business_topology_profile_snapshots(profile_id)
        ON DELETE CASCADE,
    population_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE CASCADE,
    fingerprint text NOT NULL,
    frequency bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (profile_id,fingerprint),
    CONSTRAINT business_topology_value_fingerprints_frequency_check CHECK (
        frequency > 0
    )
);
CREATE INDEX IF NOT EXISTS business_topology_value_fingerprints_lookup_idx
    ON rvbbit.business_topology_value_fingerprints
       (fingerprint,population_id);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_excavation_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'running',
    requested_by text NOT NULL DEFAULT current_user,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    sources_seen integer NOT NULL DEFAULT 0,
    sources_changed integer NOT NULL DEFAULT 0,
    populations_seen integer NOT NULL DEFAULT 0,
    profiles_created integer NOT NULL DEFAULT 0,
    jobs_enqueued integer NOT NULL DEFAULT 0,
    errors integer NOT NULL DEFAULT 0,
    error_summary text,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CONSTRAINT business_topology_excavation_runs_status_check CHECK (
        status IN ('running','complete','partial','failed')
    ),
    CONSTRAINT business_topology_excavation_runs_counts_check CHECK (
        sources_seen >= 0 AND sources_changed >= 0
        AND populations_seen >= 0 AND profiles_created >= 0
        AND jobs_enqueued >= 0 AND errors >= 0
    ),
    CONSTRAINT business_topology_excavation_runs_parameters_check CHECK (
        jsonb_typeof(parameters)='object'
    )
);
CREATE INDEX IF NOT EXISTS business_topology_excavation_runs_time_idx
    ON rvbbit.business_topology_excavation_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_excavation_sources (
    run_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_excavation_runs(run_id)
        ON DELETE CASCADE,
    source_id uuid
        REFERENCES rvbbit.business_topology_sources(source_id) ON DELETE SET NULL,
    relation_name text NOT NULL,
    status text NOT NULL,
    changed boolean NOT NULL DEFAULT false,
    populations_seen integer NOT NULL DEFAULT 0,
    profiles_created integer NOT NULL DEFAULT 0,
    jobs_enqueued integer NOT NULL DEFAULT 0,
    error text,
    profiled_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id,relation_name),
    CONSTRAINT business_topology_excavation_sources_status_check CHECK (
        status IN ('complete','skipped','failed')
    )
);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_inference_jobs (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_kind text NOT NULL,
    population_id uuid
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE CASCADE,
    peer_population_id uuid
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE CASCADE,
    input_hash text NOT NULL,
    input_packet jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    not_before timestamptz NOT NULL DEFAULT now(),
    claimed_by text,
    claimed_at timestamptz,
    model_name text,
    model_version text,
    result jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT business_topology_inference_jobs_kind_check CHECK (
        task_kind IN (
            'population_embedding','population_roles','source_motifs',
            'correspondence','entity_link'
        )
    ),
    CONSTRAINT business_topology_inference_jobs_status_check CHECK (
        status IN ('pending','running','complete','failed','stale','cancelled')
    ),
    CONSTRAINT business_topology_inference_jobs_attempts_check CHECK (
        attempts >= 0
    ),
    CONSTRAINT business_topology_inference_jobs_packet_check CHECK (
        jsonb_typeof(input_packet)='object'
        AND (result IS NULL OR jsonb_typeof(result)='object')
    ),
    CONSTRAINT business_topology_inference_jobs_distinct_pair_check CHECK (
        peer_population_id IS NULL OR peer_population_id <> population_id
    ),
    CONSTRAINT business_topology_inference_jobs_input_key
        UNIQUE (task_kind,input_hash)
);
CREATE INDEX IF NOT EXISTS business_topology_inference_jobs_queue_idx
    ON rvbbit.business_topology_inference_jobs
       (task_kind,status,not_before,created_at)
    WHERE status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS business_topology_inference_jobs_population_idx
    ON rvbbit.business_topology_inference_jobs
       (population_id,peer_population_id,created_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_proposals (
    proposal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_key text NOT NULL UNIQUE,
    proposal_kind text NOT NULL,
    payload jsonb NOT NULL,
    confidence double precision NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    source_job_id uuid
        REFERENCES rvbbit.business_topology_inference_jobs(job_id)
        ON DELETE SET NULL,
    inference_kind text NOT NULL DEFAULT 'model',
    model_name text,
    model_version text,
    proposed_by text NOT NULL DEFAULT current_user,
    proposed_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by text,
    reviewed_at timestamptz,
    review_reason text,
    materialized_ref jsonb,
    supersedes uuid
        REFERENCES rvbbit.business_topology_proposals(proposal_id)
        ON DELETE SET NULL,
    CONSTRAINT business_topology_proposals_kind_check CHECK (
        proposal_kind IN (
            'population','node','binding','edge','identity_rule','authority',
            'hierarchy'
        )
    ),
    CONSTRAINT business_topology_proposals_confidence_check CHECK (
        confidence BETWEEN 0 AND 1
    ),
    CONSTRAINT business_topology_proposals_status_check CHECK (
        status IN ('proposed','accepted','rejected','withdrawn','superseded')
    ),
    CONSTRAINT business_topology_proposals_json_check CHECK (
        jsonb_typeof(payload)='object'
        AND (materialized_ref IS NULL OR jsonb_typeof(materialized_ref)='object')
    )
);
CREATE INDEX IF NOT EXISTS business_topology_proposals_inbox_idx
    ON rvbbit.business_topology_proposals
       (status,confidence DESC,proposed_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_proposal_evidence (
    evidence_id bigserial PRIMARY KEY,
    proposal_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_proposals(proposal_id)
        ON DELETE CASCADE,
    evidence_kind text NOT NULL,
    population_id uuid
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE SET NULL,
    peer_population_id uuid
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE SET NULL,
    score double precision,
    weight double precision NOT NULL DEFAULT 1,
    source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT business_topology_proposal_evidence_kind_check CHECK (
        evidence_kind IN (
            'name_context','value_overlap','distribution','query_usage',
            'document_relation','declared_constraint','model_verdict',
            'human_decision','temporal_behavior'
        )
    ),
    CONSTRAINT business_topology_proposal_evidence_score_check CHECK (
        score IS NULL OR score BETWEEN 0 AND 1
    ),
    CONSTRAINT business_topology_proposal_evidence_weight_check CHECK (
        weight >= 0
    ),
    CONSTRAINT business_topology_proposal_evidence_json_check CHECK (
        jsonb_typeof(source_ref)='object' AND jsonb_typeof(details)='object'
    )
);
CREATE INDEX IF NOT EXISTS business_topology_proposal_evidence_proposal_idx
    ON rvbbit.business_topology_proposal_evidence
       (proposal_id,evidence_kind,evidence_id);

-- Promoted topology.  Nodes form the readable "org chart" projection;
-- bindings say where a node manifests in data; edges retain cross-cutting
-- relationships without forcing the default UI into a graph hairball.
CREATE TABLE IF NOT EXISTS rvbbit.business_topology_nodes (
    node_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    node_key text NOT NULL UNIQUE,
    node_kind text NOT NULL,
    name text NOT NULL,
    description text,
    parent_node_id uuid
        REFERENCES rvbbit.business_topology_nodes(node_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active',
    confidence double precision NOT NULL DEFAULT 1,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_proposal_id uuid
        REFERENCES rvbbit.business_topology_proposals(proposal_id)
        ON DELETE SET NULL,
    created_by text NOT NULL DEFAULT current_user,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT business_topology_nodes_kind_check CHECK (
        node_kind IN ('object','facet','lifecycle','event','measure','category')
    ),
    CONSTRAINT business_topology_nodes_status_check CHECK (
        status IN ('active','retired')
    ),
    CONSTRAINT business_topology_nodes_name_check CHECK (
        length(btrim(name)) BETWEEN 1 AND 180
    ),
    CONSTRAINT business_topology_nodes_confidence_check CHECK (
        confidence BETWEEN 0 AND 1
    ),
    CONSTRAINT business_topology_nodes_properties_check CHECK (
        jsonb_typeof(properties)='object'
    ),
    CONSTRAINT business_topology_nodes_not_self_parent_check CHECK (
        parent_node_id IS NULL OR parent_node_id <> node_id
    )
);
CREATE INDEX IF NOT EXISTS business_topology_nodes_parent_idx
    ON rvbbit.business_topology_nodes (parent_node_id,node_kind,name);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_bindings (
    node_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_nodes(node_id) ON DELETE CASCADE,
    population_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_populations(population_id)
        ON DELETE CASCADE,
    binding_role text NOT NULL,
    authority text NOT NULL DEFAULT 'unknown',
    confidence double precision NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_proposal_id uuid
        REFERENCES rvbbit.business_topology_proposals(proposal_id)
        ON DELETE SET NULL,
    accepted_by text NOT NULL DEFAULT current_user,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id,population_id,binding_role),
    CONSTRAINT business_topology_bindings_role_check CHECK (
        binding_role IN (
            'identity','attribute','event','measure','category','status',
            'time','geography','evidence','context'
        )
    ),
    CONSTRAINT business_topology_bindings_authority_check CHECK (
        authority IN ('unknown','primary','secondary','derived','conflicting')
    ),
    CONSTRAINT business_topology_bindings_confidence_check CHECK (
        confidence BETWEEN 0 AND 1
    ),
    CONSTRAINT business_topology_bindings_evidence_check CHECK (
        jsonb_typeof(evidence)='object'
    )
);
CREATE INDEX IF NOT EXISTS business_topology_bindings_population_idx
    ON rvbbit.business_topology_bindings
       (population_id,node_id,binding_role);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_edges (
    edge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_node_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_nodes(node_id) ON DELETE CASCADE,
    predicate text NOT NULL,
    object_node_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_nodes(node_id) ON DELETE CASCADE,
    confidence double precision NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_proposal_id uuid
        REFERENCES rvbbit.business_topology_proposals(proposal_id)
        ON DELETE SET NULL,
    accepted_by text NOT NULL DEFAULT current_user,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT business_topology_edges_predicate_check CHECK (
        predicate ~ '^[a-z][a-z0-9_]{0,79}$'
    ),
    CONSTRAINT business_topology_edges_confidence_check CHECK (
        confidence BETWEEN 0 AND 1
    ),
    CONSTRAINT business_topology_edges_evidence_check CHECK (
        jsonb_typeof(evidence)='object'
    ),
    CONSTRAINT business_topology_edges_distinct_nodes_check CHECK (
        subject_node_id <> object_node_id
    ),
    CONSTRAINT business_topology_edges_identity_key
        UNIQUE (subject_node_id,predicate,object_node_id)
);
CREATE INDEX IF NOT EXISTS business_topology_edges_object_idx
    ON rvbbit.business_topology_edges (object_node_id,predicate);

COMMENT ON TABLE rvbbit.business_topology_sources IS
    'Physical and unstructured source locators known to Business Topology. A source is provenance, not an asserted business object.';
COMMENT ON TABLE rvbbit.business_topology_populations IS
    'Inference atoms within sources: fields, composites, slices, streams, and mention sets. One source may contribute to many business concepts.';
COMMENT ON TABLE rvbbit.business_topology_profile_snapshots IS
    'Immutable privacy-safe population profiles plus the exact sanitized packet supplied to a specialist model.';
COMMENT ON TABLE rvbbit.business_topology_value_fingerprints IS
    'Installation-salted local value fingerprints used only for overlap/containment evidence; raw sampled values are never persisted.';
COMMENT ON TABLE rvbbit.business_topology_inference_jobs IS
    'Versioned batched-work boundary for Clover/Hutch population and correspondence specialists.';
COMMENT ON TABLE rvbbit.business_topology_nodes IS
    'Human-promoted business objects, facets, lifecycles, events, measures, and categories used for the readable company skeleton.';

-- -------------------------------------------------------------------------
-- Deterministic feature helpers.  These are weak evidence, never truth.
-- -------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit._business_topology_type_family(p_type text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT CASE
        WHEN lower(coalesce(p_type,'')) ~ '(smallint|integer|bigint|numeric|decimal|real|double)' THEN 'number'
        WHEN lower(coalesce(p_type,'')) ~ '(timestamp|date|time|interval)' THEN 'time'
        WHEN lower(coalesce(p_type,'')) ~ '(bool)' THEN 'boolean'
        WHEN lower(coalesce(p_type,'')) ~ '(json)' THEN 'document'
        WHEN lower(coalesce(p_type,'')) ~ '(bytea)' THEN 'binary'
        WHEN lower(coalesce(p_type,'')) ~ '(uuid)' THEN 'identifier'
        WHEN lower(coalesce(p_type,'')) ~ '(\[\]|array)' THEN 'array'
        WHEN lower(coalesce(p_type,'')) ~ '(char|text|citext|name)' THEN 'text'
        ELSE 'other'
    END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_value_shape(p_value text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT CASE
        WHEN p_value IS NULL THEN 'null'
        WHEN btrim(p_value)='' THEN 'empty'
        WHEN p_value ~* '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' THEN 'email'
        WHEN p_value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN 'uuid'
        WHEN p_value ~* '^https?://' THEN 'url'
        WHEN p_value ~ '^[-+]?[0-9]+$' THEN 'integer'
        WHEN p_value ~ '^[-+]?[0-9]+\.[0-9]+$' THEN 'decimal'
        WHEN p_value ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T].*)?$' THEN 'date_or_timestamp'
        WHEN lower(p_value) IN ('true','false','t','f','yes','no') THEN 'boolean'
        WHEN left(btrim(p_value),1) IN ('{','[') THEN 'structured_text'
        WHEN length(p_value) > 160 THEN 'long_text'
        WHEN p_value ~ '[[:space:]]' THEN 'phrase'
        WHEN p_value ~ '^[[:alnum:]_.:/@+-]+$' THEN 'token'
        ELSE 'text'
    END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_sensitivity_hint(
    p_name text,
    p_type text DEFAULT NULL
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT CASE
        WHEN lower(coalesce(p_name,'')) ~ '(password|passwd|secret|token|api_?key|private_?key|credential)' THEN 'restricted'
        WHEN lower(coalesce(p_name,'')) ~ '(ssn|social_?security|tax_?id|national_?id)' THEN 'restricted'
        WHEN lower(coalesce(p_name,'')) ~ '(email|e_?mail|phone|mobile|telephone)' THEN 'direct_identifier'
        WHEN lower(coalesce(p_name,'')) ~ '(first_?name|last_?name|full_?name|birth|dob|street|address)' THEN 'quasi_identifier'
        WHEN lower(coalesce(p_name,'')) ~ '(^id$|_id$|^id_|_key$|_code$)' OR lower(coalesce(p_type,''))='uuid' THEN 'identifier'
        ELSE 'unknown'
    END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_role_hints(
    p_name text,
    p_type text DEFAULT NULL
) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT coalesce(array_agg(DISTINCT role ORDER BY role),'{}'::text[])
      FROM (
        SELECT unnest(ARRAY[
            CASE WHEN lower(coalesce(p_name,'')) ~ '(^id$|_id$|^id_|_key$|_pk$|uuid|email|phone|mobile)' THEN 'identity' END,
            CASE WHEN lower(coalesce(p_name,'')) ~ '(status|state|stage|phase)' THEN 'status' END,
            CASE WHEN lower(coalesce(p_name,'')) ~ '(type|kind|category|segment|class)' THEN 'category' END,
            CASE WHEN lower(coalesce(p_name,'')) ~ '(date|time|_at$|year|month|term)'
                       OR lower(coalesce(p_type,'')) ~ '(date|time)' THEN 'time' END,
            CASE WHEN lower(coalesce(p_name,'')) ~ '(amount|price|cost|revenue|spend|margin|balance|fee|currency)' THEN 'money' END,
            CASE WHEN lower(coalesce(p_name,'')) ~ '(count|total|score|rate|ratio|percent|pct|quantity|qty)' THEN 'measure' END,
            CASE WHEN lower(coalesce(p_name,'')) ~ '(country|state|county|city|zip|postal|latitude|longitude|region|district)' THEN 'geography' END,
            CASE WHEN lower(coalesce(p_type,'')) ~ '(text|char|json)' THEN 'evidence' END
        ]) AS role
      ) hints
     WHERE role IS NOT NULL
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_name_tokens(p_name text)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT coalesce(array_agg(DISTINCT token ORDER BY token),'{}'::text[])
      FROM unnest(regexp_split_to_array(
          lower(regexp_replace(coalesce(p_name,''),'([a-z0-9])([A-Z])','\1 \2','g')),
          '[^a-z0-9]+'
      )) AS token
     WHERE token <> ''
       AND token NOT IN ('id','key','code','field','value','data','tbl','table')
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_token_overlap(
    p_left text,
    p_right text
) RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    WITH l AS (
        SELECT unnest(rvbbit._business_topology_name_tokens(p_left)) AS token
    ), r AS (
        SELECT unnest(rvbbit._business_topology_name_tokens(p_right)) AS token
    ), counts AS (
        SELECT
            (SELECT count(*) FROM (SELECT token FROM l INTERSECT SELECT token FROM r) x)::float8 AS shared,
            (SELECT count(*) FROM (SELECT token FROM l UNION SELECT token FROM r) x)::float8 AS total
    )
    SELECT CASE WHEN total=0 THEN 0 ELSE shared/total END FROM counts
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_safe_column_profile(p_column jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
AS $fn$
DECLARE
    v_shapes jsonb := '{}'::jsonb;
    v_frequency jsonb := '[]'::jsonb;
    v_type text := p_column->>'data_type';
    v_name text := p_column->>'name';
BEGIN
    SELECT coalesce(jsonb_object_agg(shape,n),'{}'::jsonb)
      INTO v_shapes
      FROM (
          SELECT rvbbit._business_topology_value_shape(e->>'value') AS shape,
                 sum(greatest(coalesce((e->>'n')::bigint,1),1))::bigint AS n
            FROM jsonb_array_elements(coalesce(p_column->'example_values','[]'::jsonb)) e
           GROUP BY 1
           ORDER BY 1
      ) shaped;

    SELECT coalesce(jsonb_agg(jsonb_build_object('n',n) ORDER BY n DESC),'[]'::jsonb)
      INTO v_frequency
      FROM (
          SELECT greatest(coalesce((e->>'n')::bigint,1),1) AS n
            FROM jsonb_array_elements(coalesce(p_column->'example_values','[]'::jsonb)) e
           ORDER BY 1 DESC
           LIMIT 64
      ) frequencies;

    RETURN jsonb_strip_nulls(jsonb_build_object(
        'name',v_name,
        'ordinal',p_column->'ordinal',
        'data_type',v_type,
        'type_family',rvbbit._business_topology_type_family(v_type),
        'nullable',p_column->'nullable',
        'comment',p_column->'comment',
        'declared_pk',coalesce((p_column->>'is_pk')::boolean,false),
        'declared_fk',coalesce((p_column->>'is_fk')::boolean,false),
        'declared_fk_target',p_column->'fk_target',
        'sample_rows',p_column->'n_seen',
        'null_fraction',p_column->'null_frac',
        'sample_distinct',p_column->'ndv',
        'cardinality_ratio',CASE
            WHEN coalesce((p_column->>'n_seen')::float8,0) <= 0 THEN NULL
            ELSE least(1.0,coalesce((p_column->>'ndv')::float8,0)
                           / (p_column->>'n_seen')::float8)
        END,
        'average_length',p_column->'average_length',
        'maximum_length',p_column->'maximum_length',
        'value_shapes',v_shapes,
        'frequency_profile',v_frequency,
        'sensitivity_hint',rvbbit._business_topology_sensitivity_hint(v_name,v_type),
        'role_hints',to_jsonb(rvbbit._business_topology_role_hints(v_name,v_type)),
        'name_tokens',to_jsonb(rvbbit._business_topology_name_tokens(v_name))
    ));
END
$fn$;

-- A bounded replacement for treating catalog_fingerprint_table() as a semantic
-- crawler.  It never runs count(*) against the source, never ORDER BY random(),
-- and materializes at most p_sample_rows before computing all field features.
-- Exact values exist only in this transient return value so the storing wrapper
-- can salt/hash them; no raw value is inserted by this function.
CREATE OR REPLACE FUNCTION rvbbit._business_topology_raw_profile(
    p_relation regclass,
    p_sample_rows integer DEFAULT 2048,
    p_value_cap integer DEFAULT 128,
    p_seed integer DEFAULT 1729
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_schema text;
    v_relation text;
    v_relkind "char";
    v_comment text;
    v_size_bytes bigint;
    v_row_estimate bigint;
    v_relpages integer;
    v_sample_rows bigint := 0;
    v_sampling_method text;
    v_sample_pct numeric;
    v_columns jsonb := '[]'::jsonb;
    v_pair_features jsonb := '[]'::jsonb;
    v_pair_values_sql text;
    v_pair_fields integer := 0;
    v_column record;
    v_seen bigint;
    v_nonnull bigint;
    v_distinct bigint;
    v_avg_length double precision;
    v_max_length integer;
    v_value_dist jsonb;
    v_examples jsonb;
BEGIN
    IF p_sample_rows NOT BETWEEN 32 AND 50000 THEN
        RAISE EXCEPTION
            'business topology sample_rows must be between 32 and 50000 (got %)',
            p_sample_rows;
    END IF;
    IF p_value_cap NOT BETWEEN 8 AND 256 THEN
        RAISE EXCEPTION
            'business topology value_cap must be between 8 and 256 (got %)',
            p_value_cap;
    END IF;
    IF NOT has_table_privilege(p_relation,'SELECT') THEN
        RAISE EXCEPTION 'SELECT privilege is required to profile %',p_relation;
    END IF;

    -- Internal profiling queries always use the ordinary Postgres path.  They
    -- run over a bounded heap temp table after the first read and should not
    -- incur a novel-shape routing decision for every aggregate.
    PERFORM set_config('rvbbit.force_heap_scan','on',true);

    SELECT n.nspname,c.relname,c.relkind,obj_description(c.oid,'pg_class'),
           pg_total_relation_size(c.oid),
           greatest(coalesce(c.reltuples,0),0)::bigint,
           greatest(coalesce(c.relpages,0),0)
      INTO v_schema,v_relation,v_relkind,v_comment,v_size_bytes,
           v_row_estimate,v_relpages
      FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE c.oid=p_relation;

    IF NOT FOUND OR v_relkind NOT IN ('r','p','m','v','f') THEN
        RAISE EXCEPTION '% is not a profileable relation',p_relation;
    END IF;

    IF to_regclass('pg_temp._business_topology_sample') IS NOT NULL THEN
        EXECUTE 'DROP TABLE _business_topology_sample';
    END IF;
    IF to_regclass('pg_temp._business_topology_cells') IS NOT NULL THEN
        EXECUTE 'DROP TABLE _business_topology_cells';
    END IF;

    -- Unknown statistics on a large heap must not trigger a full scan.  A
    -- deterministic page sample is good enough for discovery and is retried
    -- with a bounded LIMIT only when it happens to land on zero rows.
    IF v_relkind IN ('r','m')
       AND (
           v_row_estimate > p_sample_rows
           OR (v_row_estimate=0 AND v_relpages > 128)
       ) THEN
        v_sample_pct := CASE
            WHEN v_row_estimate > 0 THEN
                greatest(0.001,least(100.0,
                    200.0*p_sample_rows/greatest(v_row_estimate,1)))
            ELSE 1.0
        END;
        BEGIN
            EXECUTE format(
                'CREATE TEMP TABLE _business_topology_sample ON COMMIT DROP '
                || 'AS SELECT * FROM %s TABLESAMPLE SYSTEM (%s) REPEATABLE (%s) LIMIT %s',
                p_relation,v_sample_pct,p_seed,p_sample_rows
            );
            v_sampling_method := 'system_sample';
        EXCEPTION WHEN OTHERS THEN
            -- Some foreign/custom table AMs reject TABLESAMPLE.  The fallback
            -- still reads/retains at most p_sample_rows and preserves caller
            -- statement_timeout/cancellation semantics.
            IF to_regclass('pg_temp._business_topology_sample') IS NOT NULL THEN
                EXECUTE 'DROP TABLE _business_topology_sample';
            END IF;
            EXECUTE format(
                'CREATE TEMP TABLE _business_topology_sample ON COMMIT DROP '
                || 'AS SELECT * FROM %s LIMIT %s',
                p_relation,p_sample_rows
            );
            v_sampling_method := 'bounded_limit_sample_fallback';
        END;
    ELSE
        EXECUTE format(
            'CREATE TEMP TABLE _business_topology_sample ON COMMIT DROP '
            || 'AS SELECT * FROM %s LIMIT %s',
            p_relation,p_sample_rows
        );
        v_sampling_method := 'bounded_limit';
    END IF;

    SELECT count(*) INTO v_sample_rows FROM _business_topology_sample;
    IF v_sample_rows=0 AND (v_row_estimate > 0 OR v_size_bytes > 0) THEN
        EXECUTE 'DROP TABLE _business_topology_sample';
        EXECUTE format(
            'CREATE TEMP TABLE _business_topology_sample ON COMMIT DROP '
            || 'AS SELECT * FROM %s LIMIT %s',
            p_relation,p_sample_rows
        );
        v_sampling_method := 'bounded_limit_fallback';
        SELECT count(*) INTO v_sample_rows FROM _business_topology_sample;
    END IF;

    FOR v_column IN
        SELECT a.attnum,
               a.attname,
               format_type(a.atttypid,a.atttypmod) AS data_type,
               NOT a.attnotnull AS nullable,
               col_description(a.attrelid,a.attnum) AS comment,
               EXISTS (
                   SELECT 1 FROM pg_constraint pc
                    WHERE pc.conrelid=a.attrelid
                      AND pc.contype='p'
                      AND a.attnum=ANY(pc.conkey)
               ) AS is_pk,
               (
                   SELECT cn.nspname||'.'||cf.relname||'.'||af.attname
                     FROM pg_constraint fc
                     JOIN pg_class cf ON cf.oid=fc.confrelid
                     JOIN pg_namespace cn ON cn.oid=cf.relnamespace
                     JOIN pg_attribute af
                       ON af.attrelid=fc.confrelid
                      AND af.attnum=fc.confkey[array_position(fc.conkey,a.attnum)]
                    WHERE fc.conrelid=a.attrelid
                      AND fc.contype='f'
                      AND a.attnum=ANY(fc.conkey)
                    LIMIT 1
               ) AS fk_target
          FROM pg_attribute a
         WHERE a.attrelid=p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
         ORDER BY a.attnum
    LOOP
        EXECUTE format(
            'SELECT count(*),count(%1$I),'
            || 'count(DISTINCT left(%1$I::text,512)),'
            || 'avg(length(left(%1$I::text,512))),'
            || 'max(length(left(%1$I::text,512))) '
            || 'FROM _business_topology_sample',
            v_column.attname
        )
        INTO v_seen,v_nonnull,v_distinct,v_avg_length,v_max_length;

        v_value_dist := NULL;
        BEGIN
            EXECUTE format(
                'SELECT jsonb_object_agg(v,n) FROM ('
                || ' SELECT left(%1$I::text,512) AS v,count(*)::bigint AS n'
                || ' FROM _business_topology_sample'
                || ' WHERE %1$I IS NOT NULL'
                || ' GROUP BY 1 ORDER BY count(*) DESC,1 LIMIT %2$s'
                || ') values_ranked',
                v_column.attname,p_value_cap
            ) INTO v_value_dist;
        EXCEPTION WHEN OTHERS THEN
            v_value_dist := NULL;
        END;

        SELECT coalesce(
                   jsonb_agg(
                       jsonb_build_object('value',value,'n',n)
                       ORDER BY n DESC,value
                   ),
                   '[]'::jsonb
               )
          INTO v_examples
          FROM (
              SELECT e.key AS value,(e.value)::bigint AS n
                FROM jsonb_each_text(coalesce(v_value_dist,'{}'::jsonb)) e
               ORDER BY (e.value)::bigint DESC,e.key
               LIMIT 64
          ) ranked;

        v_columns := v_columns || jsonb_build_object(
            'name',v_column.attname,
            'ordinal',v_column.attnum,
            'data_type',v_column.data_type,
            'nullable',v_column.nullable,
            'comment',v_column.comment,
            'is_pk',v_column.is_pk,
            'is_fk',(v_column.fk_target IS NOT NULL),
            'fk_target',v_column.fk_target,
            'n_seen',v_seen,
            'n_nulls',v_seen-v_nonnull,
            'null_frac',CASE WHEN v_seen=0 THEN NULL
                             ELSE (v_seen-v_nonnull)::float8/v_seen END,
            'ndv',v_distinct,
            'average_length',v_avg_length,
            'maximum_length',v_max_length,
            'example_values',v_examples,
            'value_dist',v_value_dist
        );
    END LOOP;

    -- A relation is not a semantic container, but rows still carry valuable
    -- archaeological evidence about which fields move together.  Unpivot at
    -- most 48 fields from the already bounded sample, then calculate numeric
    -- pair features in one set-based pass.  No cell value survives the temp
    -- table or enters the returned packet.
    BEGIN
        SELECT string_agg(format(
                   '(%s,%L,CASE WHEN s.%I IS NULL THEN NULL '
                   || 'ELSE left(s.%I::text,512) END)',
                   selected.attnum,selected.attname,
                   selected.attname,selected.attname
               ),',' ORDER BY selected.rank_order),
               count(*)::integer
          INTO v_pair_values_sql,v_pair_fields
          FROM (
              SELECT a.attnum,a.attname,
                     row_number() OVER (
                         ORDER BY CASE
                             WHEN rvbbit._business_topology_type_family(
                                 format_type(a.atttypid,a.atttypmod)
                             ) IN ('document','binary','array') THEN 1
                             ELSE 0
                         END,
                         a.attnum
                     ) AS rank_order
                FROM pg_attribute a
               WHERE a.attrelid=p_relation
                 AND a.attnum > 0
                 AND NOT a.attisdropped
               ORDER BY CASE
                   WHEN rvbbit._business_topology_type_family(
                       format_type(a.atttypid,a.atttypmod)
                   ) IN ('document','binary','array') THEN 1
                   ELSE 0
               END,
               a.attnum
               LIMIT 48
          ) selected;

        IF v_sample_rows > 0 AND nullif(v_pair_values_sql,'') IS NOT NULL THEN
            EXECUTE format(
                'CREATE TEMP TABLE _business_topology_cells ON COMMIT DROP AS '
                || 'SELECT s.ctid::text AS sample_row,cell.ordinal,'
                || 'cell.field_name,cell.value_text '
                || 'FROM _business_topology_sample s '
                || 'CROSS JOIN LATERAL (VALUES %s) '
                || 'AS cell(ordinal,field_name,value_text)',
                v_pair_values_sql
            );

            WITH pair_stats AS (
                SELECT a.ordinal AS left_ordinal,
                       b.ordinal AS right_ordinal,
                       min(a.field_name) AS left_field,
                       min(b.field_name) AS right_field,
                       count(*)::bigint AS rows_seen,
                       count(*) FILTER (
                           WHERE a.value_text IS NOT NULL
                       )::bigint AS left_present,
                       count(*) FILTER (
                           WHERE b.value_text IS NOT NULL
                       )::bigint AS right_present,
                       count(*) FILTER (
                           WHERE a.value_text IS NOT NULL
                             AND b.value_text IS NOT NULL
                       )::bigint AS both_present,
                       count(*) FILTER (
                           WHERE (a.value_text IS NULL)=(b.value_text IS NULL)
                       )::bigint AS same_presence,
                       count(DISTINCT a.value_text) FILTER (
                           WHERE a.value_text IS NOT NULL
                             AND b.value_text IS NOT NULL
                       )::bigint AS left_distinct,
                       count(DISTINCT b.value_text) FILTER (
                           WHERE a.value_text IS NOT NULL
                             AND b.value_text IS NOT NULL
                       )::bigint AS right_distinct,
                       count(DISTINCT (a.value_text,b.value_text)) FILTER (
                           WHERE a.value_text IS NOT NULL
                             AND b.value_text IS NOT NULL
                       )::bigint AS pair_distinct,
                       count(*) FILTER (
                           WHERE a.value_text IS NOT NULL
                             AND b.value_text IS NOT NULL
                             AND a.value_text=b.value_text
                       )::bigint AS equal_values
                  FROM _business_topology_cells a
                  JOIN _business_topology_cells b
                    ON b.sample_row=a.sample_row
                   AND b.ordinal>a.ordinal
                 GROUP BY a.ordinal,b.ordinal
            )
            SELECT coalesce(jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                       'left',left_field,
                       'right',right_field,
                       'rows_seen',rows_seen,
                       'both_present_fraction',CASE
                           WHEN rows_seen=0 THEN NULL
                           ELSE both_present::float8/rows_seen
                       END,
                       'same_presence_fraction',CASE
                           WHEN rows_seen=0 THEN NULL
                           ELSE same_presence::float8/rows_seen
                       END,
                       'presence_lift',CASE
                           WHEN left_present=0 OR right_present=0 THEN NULL
                           ELSE both_present::float8*rows_seen
                                /(left_present::float8*right_present)
                       END,
                       'left_to_right_strength',CASE
                           WHEN pair_distinct=0 THEN NULL
                           ELSE left_distinct::float8/pair_distinct
                       END,
                       'right_to_left_strength',CASE
                           WHEN pair_distinct=0 THEN NULL
                           ELSE right_distinct::float8/pair_distinct
                       END,
                       'equal_value_fraction',CASE
                           WHEN both_present=0 THEN NULL
                           ELSE equal_values::float8/both_present
                       END,
                       'pair_distinct',pair_distinct
                   )) ORDER BY left_ordinal,right_ordinal),'[]'::jsonb)
              INTO v_pair_features
              FROM pair_stats;

            EXECUTE 'DROP TABLE _business_topology_cells';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        v_pair_features := '[]'::jsonb;
        IF to_regclass('pg_temp._business_topology_cells') IS NOT NULL THEN
            EXECUTE 'DROP TABLE _business_topology_cells';
        END IF;
    END;

    IF to_regclass('pg_temp._business_topology_sample') IS NOT NULL THEN
        EXECUTE 'DROP TABLE _business_topology_sample';
    END IF;

    RETURN jsonb_strip_nulls(jsonb_build_object(
        'relation',p_relation::text,
        'relation_oid',(p_relation::oid)::text,
        'database',current_database(),
        'schema',v_schema,
        'name',v_relation,
        'relation_kind',v_relkind::text,
        'comment',v_comment,
        'size_bytes',v_size_bytes,
        'row_estimate',v_row_estimate,
        'row_estimate_known',(v_row_estimate > 0),
        'sample_rows',v_sample_rows,
        'sample_limit',p_sample_rows,
        'sampling_method',v_sampling_method,
        'columns',v_columns,
        'pair_field_limit',48,
        'pair_fields_profiled',v_pair_fields,
        'field_pairs',v_pair_features
    ));
EXCEPTION WHEN OTHERS THEN
    BEGIN
        IF to_regclass('pg_temp._business_topology_sample') IS NOT NULL THEN
            EXECUTE 'DROP TABLE _business_topology_sample';
        END IF;
        IF to_regclass('pg_temp._business_topology_cells') IS NOT NULL THEN
            EXECUTE 'DROP TABLE _business_topology_cells';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        NULL;
    END;
    RAISE;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_packet_from_raw(p_raw jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    WITH safe_columns AS (
        SELECT coalesce(
                   jsonb_agg(
                       rvbbit._business_topology_safe_column_profile(c)
                       ORDER BY (c->>'ordinal')::integer
                   ),
                   '[]'::jsonb
               ) AS columns
          FROM jsonb_array_elements(coalesce(p_raw->'columns','[]'::jsonb)) c
    )
    SELECT jsonb_strip_nulls(jsonb_build_object(
        'schema_version','rvbbit.business-topology.profile-packet.v1',
        'privacy',jsonb_build_object(
            'raw_values',false,
            'value_hashes',false,
            'bounded_sample',true
        ),
        'source',jsonb_build_object(
            'kind','postgres_relation',
            'database',p_raw->'database',
            'schema',p_raw->'schema',
            'relation',p_raw->'name',
            'relation_kind',p_raw->'relation_kind',
            'comment',p_raw->'comment'
        ),
        'sample',jsonb_build_object(
            'rows',p_raw->'sample_rows',
            'limit',p_raw->'sample_limit',
            'method',p_raw->'sampling_method',
            'row_estimate',p_raw->'row_estimate',
            'row_estimate_known',p_raw->'row_estimate_known'
        ),
        'relation_context',jsonb_build_object(
            'name',p_raw->'name',
            'name_tokens',to_jsonb(
                rvbbit._business_topology_name_tokens(p_raw->>'name')
            ),
            'field_count',jsonb_array_length(columns),
            'fields',columns,
            'pair_field_limit',p_raw->'pair_field_limit',
            'pair_fields_profiled',p_raw->'pair_fields_profiled',
            'field_pairs',coalesce(p_raw->'field_pairs','[]'::jsonb)
        )
    ))
      FROM safe_columns
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_profile_packet(
    p_relation regclass,
    p_sample_rows integer DEFAULT 2048
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_raw jsonb;
BEGIN
    v_raw := rvbbit._business_topology_raw_profile(
        p_relation,p_sample_rows,128,1729
    );
    RETURN rvbbit._business_topology_packet_from_raw(v_raw);
END
$fn$;

COMMENT ON FUNCTION rvbbit.business_topology_profile_packet(regclass,integer) IS
    'Build a bounded privacy-safe v1 topology packet for one caller-readable relation. No raw values or value hashes are returned or persisted.';

-- Profile and persist one relation.  Automatic populations are intentionally
-- atomic fields plus one context-only relation population.  Composite/slice
-- populations are added later as proposals supported by field-level evidence;
-- this function never asserts that the relation itself is one business object.
CREATE OR REPLACE FUNCTION rvbbit.business_topology_excavate_relation(
    p_relation regclass,
    p_sample_rows integer DEFAULT 2048,
    p_enqueue boolean DEFAULT true,
    p_force boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_raw jsonb;
    v_packet jsonb;
    v_source_id uuid;
    v_source_key text;
    v_source_locator jsonb;
    v_salt text;
    v_column jsonb;
    v_safe_column jsonb;
    v_population_id uuid;
    v_population_key text;
    v_profile_id bigint;
    v_old_profile_id bigint;
    v_profile jsonb;
    v_model_packet jsonb;
    v_fingerprints jsonb;
    v_value_signature text;
    v_profile_hash text;
    v_model_input_hash text;
    v_context_profile jsonb;
    v_motif_packet jsonb;
    v_motif_input_hash text;
    v_context_population_id uuid;
    v_context_profile_id bigint;
    v_neighbors jsonb;
    v_field_pairs jsonb;
    v_field_hashes jsonb := '{}'::jsonb;
    v_profile_created boolean;
    v_changed boolean := false;
    v_populations integer := 0;
    v_profiles integer := 0;
    v_jobs integer := 0;
    v_affected integer;
BEGIN
    SELECT value #>> '{}'
      INTO v_salt
      FROM rvbbit.settings
     WHERE key='business_topology_profile_salt';
    IF nullif(v_salt,'') IS NULL THEN
        RAISE EXCEPTION 'business topology profile salt is not configured';
    END IF;

    v_raw := rvbbit._business_topology_raw_profile(
        p_relation,p_sample_rows,128,1729
    );
    v_packet := rvbbit._business_topology_packet_from_raw(v_raw);
    v_source_key := 'postgres_relation:' || md5(
        coalesce(v_raw->>'database','') || chr(31)
        || coalesce(v_raw->>'schema','') || chr(31)
        || coalesce(v_raw->>'name','')
    );
    v_source_locator := jsonb_build_object(
        'database',v_raw->'database',
        'schema',v_raw->'schema',
        'relation',v_raw->'name',
        'relation_oid',v_raw->'relation_oid'
    );

    INSERT INTO rvbbit.business_topology_sources (
        source_key,source_kind,locator,relation_oid,schema_name,
        relation_name,relation_kind,status,last_seen_at,properties
    ) VALUES (
        v_source_key,'postgres_relation',v_source_locator,p_relation::oid,
        v_raw->>'schema',v_raw->>'name',v_raw->>'relation_kind','active',now(),
        jsonb_build_object(
            'comment',v_raw->'comment',
            'size_bytes',v_raw->'size_bytes',
            'row_estimate',v_raw->'row_estimate'
        )
    )
    ON CONFLICT (source_key) DO UPDATE SET
        locator=EXCLUDED.locator,
        relation_oid=EXCLUDED.relation_oid,
        schema_name=EXCLUDED.schema_name,
        relation_name=EXCLUDED.relation_name,
        relation_kind=EXCLUDED.relation_kind,
        status='active',
        retired_at=NULL,
        last_seen_at=now(),
        properties=EXCLUDED.properties
    RETURNING source_id INTO v_source_id;

    -- Anything not observed in this relation snapshot becomes stale.  The loop
    -- below reactivates every current field.  Composite/slice populations are
    -- not touched because they have their own selectors and review lifecycle.
    UPDATE rvbbit.business_topology_populations
       SET status='stale',updated_at=now()
     WHERE source_id=v_source_id
       AND population_kind='field'
       AND status='active';

    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'name',f->>'name',
               'data_type',f->>'data_type',
               'type_family',f->>'type_family',
               'role_hints',f->'role_hints',
               'sensitivity_hint',f->>'sensitivity_hint'
           ) ORDER BY (f->>'ordinal')::integer),'[]'::jsonb)
      INTO v_neighbors
      FROM (
          SELECT f
            FROM jsonb_array_elements(
                v_packet #> '{relation_context,fields}'
            ) f
           ORDER BY (f->>'ordinal')::integer
           LIMIT 96
      ) bounded_fields;

    FOR v_column IN
        SELECT c
          FROM jsonb_array_elements(coalesce(v_raw->'columns','[]'::jsonb)) c
         ORDER BY (c->>'ordinal')::integer
    LOOP
        v_safe_column := rvbbit._business_topology_safe_column_profile(v_column);
        v_population_key := v_source_key || '#field:' || (v_column->>'name');

        INSERT INTO rvbbit.business_topology_populations (
            source_id,population_key,population_kind,selector,display_name,
            status,updated_at
        ) VALUES (
            v_source_id,v_population_key,'field',
            jsonb_build_object(
                'kind','field',
                'columns',jsonb_build_array(v_column->>'name')
            ),
            (v_raw->>'schema')||'.'||(v_raw->>'name')||'.'||(v_column->>'name'),
            'active',now()
        )
        ON CONFLICT (population_key) DO UPDATE SET
            source_id=EXCLUDED.source_id,
            selector=EXCLUDED.selector,
            display_name=EXCLUDED.display_name,
            status='active',
            updated_at=now()
        RETURNING population_id,current_profile_id
             INTO v_population_id,v_old_profile_id;

        -- Collapse values that normalize to the same salted fingerprint.  The
        -- JSON array is transient and is inserted only into the private local
        -- fingerprint table after the profile row exists.
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'fingerprint',fingerprint,'frequency',frequency
               ) ORDER BY frequency DESC,fingerprint),'[]'::jsonb)
          INTO v_fingerprints
          FROM (
              SELECT md5(
                         v_salt || chr(31) || lower(btrim(e.key))
                     ) AS fingerprint,
                     sum((e.value)::bigint)::bigint AS frequency
                FROM jsonb_each_text(
                    coalesce(v_column->'value_dist','{}'::jsonb)
                ) e
               WHERE btrim(e.key) <> ''
               GROUP BY 1
               ORDER BY sum((e.value)::bigint) DESC,1
               LIMIT 128
          ) fingerprinted;
        v_value_signature := md5(v_fingerprints::text);

        v_profile := jsonb_build_object(
            'schema_version','rvbbit.business-topology.local-profile.v1',
            'population',jsonb_build_object(
                'population_key',v_population_key,
                'kind','field',
                'selector',jsonb_build_object(
                    'columns',jsonb_build_array(v_column->>'name')
                )
            ),
            'source',v_packet->'source',
            'sample',v_packet->'sample',
            'field',v_safe_column,
            'local_evidence',jsonb_build_object(
                'value_fingerprint_count',jsonb_array_length(v_fingerprints),
                'value_fingerprint_signature',v_value_signature
            )
        );
        SELECT coalesce(jsonb_agg(pair),'[]'::jsonb)
          INTO v_field_pairs
          FROM jsonb_array_elements(coalesce(
              v_packet #> '{relation_context,field_pairs}','[]'::jsonb
          )) pair
         WHERE pair->>'left'=v_column->>'name'
            OR pair->>'right'=v_column->>'name';
        v_model_packet := jsonb_build_object(
            'schema_version','rvbbit.business-topology.population.v1',
            'privacy',v_packet->'privacy',
            'population',jsonb_build_object(
                'population_key',v_population_key,
                'kind','field',
                'selector',jsonb_build_object(
                    'columns',jsonb_build_array(v_column->>'name')
                )
            ),
            'source',v_packet->'source',
            'sample',v_packet->'sample',
            'field',v_safe_column,
            'neighbor_fields',v_neighbors,
            'field_pair_features',v_field_pairs
        );
        v_model_input_hash := md5(v_model_packet::text);
        -- The immutable snapshot receipts both local evidence and the exact
        -- outbound model packet.  A neighboring field can change the model's
        -- interpretation even when this field's own distribution is stable.
        v_profile_hash := md5(
            v_profile::text || chr(31) || v_model_input_hash
        );
        v_profile_created := false;

        INSERT INTO rvbbit.business_topology_profile_snapshots (
            population_id,packet_version,profile_hash,model_input_hash,
            source_fingerprint,sample_rows,sampling_method,profile,model_packet
        ) VALUES (
            v_population_id,'rvbbit.business-topology.population.v1',
            v_profile_hash,v_model_input_hash,md5(v_profile::text||v_salt),
            coalesce((v_raw->>'sample_rows')::integer,0),
            coalesce(v_raw->>'sampling_method','unknown'),
            v_profile,v_model_packet
        )
        ON CONFLICT (population_id,profile_hash) DO NOTHING
        RETURNING profile_id INTO v_profile_id;

        IF v_profile_id IS NULL THEN
            SELECT profile_id
              INTO v_profile_id
              FROM rvbbit.business_topology_profile_snapshots
             WHERE population_id=v_population_id
               AND profile_hash=v_profile_hash;
        ELSE
            v_profile_created := true;
            v_profiles := v_profiles+1;

            INSERT INTO rvbbit.business_topology_value_fingerprints (
                profile_id,population_id,fingerprint,frequency
            )
            SELECT v_profile_id,v_population_id,
                   e->>'fingerprint',(e->>'frequency')::bigint
              FROM jsonb_array_elements(v_fingerprints) e
            ON CONFLICT (profile_id,fingerprint) DO UPDATE SET
                frequency=EXCLUDED.frequency;
        END IF;

        IF v_old_profile_id IS DISTINCT FROM v_profile_id THEN
            v_changed := true;
            UPDATE rvbbit.business_topology_inference_jobs
               SET status='stale',completed_at=now(),
                   error='one of the correspondence profiles changed'
             WHERE task_kind='correspondence'
               AND (
                   population_id=v_population_id
                   OR peer_population_id=v_population_id
               )
               AND status IN ('pending','running','failed');
        END IF;
        UPDATE rvbbit.business_topology_populations
           SET current_profile_id=v_profile_id,
               last_profiled_at=now(),
               updated_at=now(),
               status='active'
         WHERE population_id=v_population_id;

        v_field_hashes := v_field_hashes || jsonb_build_object(
            v_column->>'name',v_profile_hash
        );
        v_populations := v_populations+1;

        -- A newer packet makes unclaimed work for an older packet irrelevant.
        UPDATE rvbbit.business_topology_inference_jobs
           SET status='stale',completed_at=now(),
               error='superseded by a newer population profile'
         WHERE population_id=v_population_id
           AND peer_population_id IS NULL
           AND task_kind IN ('population_embedding','population_roles')
           AND input_hash<>v_model_input_hash
           AND status IN ('pending','running','failed');

        IF p_enqueue THEN
            INSERT INTO rvbbit.business_topology_inference_jobs (
                task_kind,population_id,input_hash,input_packet
            ) VALUES (
                'population_embedding',v_population_id,
                v_model_input_hash,v_model_packet
            )
            ON CONFLICT (task_kind,input_hash) DO NOTHING;
            GET DIAGNOSTICS v_affected=ROW_COUNT;
            v_jobs := v_jobs+v_affected;

            IF p_force AND v_affected=0 THEN
                UPDATE rvbbit.business_topology_inference_jobs
                   SET status='pending',not_before=now(),claimed_by=NULL,
                       claimed_at=NULL,error=NULL,completed_at=NULL
                 WHERE task_kind='population_embedding'
                   AND input_hash=v_model_input_hash
                   AND status IN ('failed','stale','cancelled');
                GET DIAGNOSTICS v_affected=ROW_COUNT;
                v_jobs := v_jobs+v_affected;
            END IF;
        END IF;

        -- Prevent a previous loop value from being mistaken for a RETURNING
        -- result when the next immutable profile already exists.
        v_profile_id := NULL;
    END LOOP;

    -- The relation-context population exists only to carry neighborhood and
    -- source semantics into the encoder.  It is never promoted as a business
    -- object merely because the physical table exists.
    v_population_key := v_source_key || '#context';
    INSERT INTO rvbbit.business_topology_populations (
        source_id,population_key,population_kind,selector,display_name,
        status,updated_at
    ) VALUES (
        v_source_id,v_population_key,'record_context',
        jsonb_build_object('kind','record_context','context_only',true),
        (v_raw->>'schema')||'.'||(v_raw->>'name')||' context',
        'active',now()
    )
    ON CONFLICT (population_key) DO UPDATE SET
        source_id=EXCLUDED.source_id,
        selector=EXCLUDED.selector,
        display_name=EXCLUDED.display_name,
        status='active',
        updated_at=now()
    RETURNING population_id,current_profile_id
         INTO v_context_population_id,v_old_profile_id;

    v_model_packet := jsonb_build_object(
        'schema_version','rvbbit.business-topology.population.v1',
        'privacy',v_packet->'privacy',
        'population',jsonb_build_object(
            'population_key',v_population_key,
            'kind','record_context',
            'context_only',true
        ),
        'source',v_packet->'source',
        'sample',v_packet->'sample',
        'relation_context',v_packet->'relation_context'
    );
    v_context_profile := v_model_packet || jsonb_build_object(
        'local_evidence',jsonb_build_object(
            'field_profile_hashes',v_field_hashes
        )
    );
    v_profile_hash := md5(v_context_profile::text);
    v_model_input_hash := md5(v_model_packet::text);
    v_context_profile_id := NULL;

    INSERT INTO rvbbit.business_topology_profile_snapshots (
        population_id,packet_version,profile_hash,model_input_hash,
        source_fingerprint,sample_rows,sampling_method,profile,model_packet
    ) VALUES (
        v_context_population_id,'rvbbit.business-topology.population.v1',
        v_profile_hash,v_model_input_hash,md5(v_context_profile::text||v_salt),
        coalesce((v_raw->>'sample_rows')::integer,0),
        coalesce(v_raw->>'sampling_method','unknown'),
        v_context_profile,v_model_packet
    )
    ON CONFLICT (population_id,profile_hash) DO NOTHING
    RETURNING profile_id INTO v_context_profile_id;

    IF v_context_profile_id IS NULL THEN
        SELECT profile_id
          INTO v_context_profile_id
          FROM rvbbit.business_topology_profile_snapshots
         WHERE population_id=v_context_population_id
           AND profile_hash=v_profile_hash;
    ELSE
        v_profiles := v_profiles+1;
    END IF;
    IF v_old_profile_id IS DISTINCT FROM v_context_profile_id THEN
        v_changed := true;
    END IF;
    UPDATE rvbbit.business_topology_populations
       SET current_profile_id=v_context_profile_id,last_profiled_at=now(),
           updated_at=now(),status='active'
     WHERE population_id=v_context_population_id;
    v_populations := v_populations+1;

    UPDATE rvbbit.business_topology_inference_jobs
       SET status='stale',completed_at=now(),
           error='superseded by a newer population profile'
     WHERE population_id=v_context_population_id
       AND peer_population_id IS NULL
       AND task_kind IN ('population_embedding','population_roles')
       AND input_hash<>v_model_input_hash
       AND status IN ('pending','running','failed');
    IF p_enqueue THEN
        INSERT INTO rvbbit.business_topology_inference_jobs (
            task_kind,population_id,input_hash,input_packet
        ) VALUES (
            'population_embedding',v_context_population_id,
            v_model_input_hash,v_model_packet
        )
        ON CONFLICT (task_kind,input_hash) DO NOTHING;
        GET DIAGNOSTICS v_affected=ROW_COUNT;
        v_jobs := v_jobs+v_affected;

        -- The context packet is also the bounded input for discovering several
        -- candidate populations inside one physical relation.  This task must
        -- return declarative field bundles/slices, never assert that the table
        -- itself is one business object and never return executable SQL.
        v_motif_packet := jsonb_build_object(
            'schema_version','rvbbit.business-topology.source-motifs.v1',
            'privacy',v_packet->'privacy',
            'source',v_packet->'source',
            'sample',v_packet->'sample',
            'relation_context',v_packet->'relation_context',
            'output_contract',jsonb_build_object(
                'populations','declarative field bundles or slices',
                'allow_sql',false,
                'allow_multiple_objects_per_source',true,
                'allow_abstain',true
            )
        );
        v_motif_input_hash := md5(v_motif_packet::text);
        UPDATE rvbbit.business_topology_inference_jobs
           SET status='stale',completed_at=now(),
               error='superseded by a newer source motif packet'
         WHERE task_kind='source_motifs'
           AND population_id=v_context_population_id
           AND input_hash<>v_motif_input_hash
           AND status IN ('pending','running','failed');
        INSERT INTO rvbbit.business_topology_inference_jobs (
            task_kind,population_id,input_hash,input_packet
        ) VALUES (
            'source_motifs',v_context_population_id,
            v_motif_input_hash,v_motif_packet
        )
        ON CONFLICT (task_kind,input_hash) DO NOTHING;
        GET DIAGNOSTICS v_affected=ROW_COUNT;
        v_jobs := v_jobs+v_affected;
    END IF;

    UPDATE rvbbit.business_topology_sources
       SET source_fingerprint=v_profile_hash,
           last_profiled_at=now(),
           last_seen_at=now(),
           status='active'
     WHERE source_id=v_source_id;

    RETURN jsonb_build_object(
        'source_id',v_source_id,
        'source_key',v_source_key,
        'relation',p_relation::text,
        'changed',v_changed,
        'populations_seen',v_populations,
        'profiles_created',v_profiles,
        'jobs_enqueued',v_jobs,
        'sample_rows',coalesce((v_raw->>'sample_rows')::integer,0),
        'sampling_method',v_raw->>'sampling_method'
    );
END
$fn$;

COMMENT ON FUNCTION rvbbit.business_topology_excavate_relation(regclass,integer,boolean,boolean) IS
    'Profile one readable relation into atomic field populations plus context. Bounded, incremental, raw-value-free, and optionally queues versioned Clover work.';

CREATE OR REPLACE FUNCTION rvbbit.business_topology_excavation_run(
    p_relations text[] DEFAULT NULL,
    p_schemas text[] DEFAULT NULL,
    p_sample_rows integer DEFAULT 2048,
    p_max_relations integer DEFAULT 100,
    p_enqueue boolean DEFAULT true,
    p_force boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_run_id uuid;
    v_candidate record;
    v_relation regclass;
    v_result jsonb;
    v_sources integer := 0;
    v_changed integer := 0;
    v_populations integer := 0;
    v_profiles integer := 0;
    v_jobs integer := 0;
    v_correspondence_jobs integer := 0;
    v_errors integer := 0;
    v_error_messages text[] := '{}';
BEGIN
    IF p_sample_rows NOT BETWEEN 32 AND 50000 THEN
        RAISE EXCEPTION 'sample_rows must be between 32 and 50000';
    END IF;
    IF p_max_relations NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'max_relations must be between 1 and 1000';
    END IF;

    INSERT INTO rvbbit.business_topology_excavation_runs (
        parameters
    ) VALUES (jsonb_build_object(
        'relations',to_jsonb(p_relations),
        'schemas',to_jsonb(p_schemas),
        'sample_rows',p_sample_rows,
        'max_relations',p_max_relations,
        'enqueue',p_enqueue,
        'force',p_force,
        'profile_packet_version','rvbbit.business-topology.profile-packet.v1'
    ))
    RETURNING run_id INTO v_run_id;

    FOR v_candidate IN
        WITH explicit AS (
            SELECT u.relation_name,u.ordinality
              FROM unnest(p_relations) WITH ORDINALITY
                   AS u(relation_name,ordinality)
             WHERE p_relations IS NOT NULL
        ), discovered AS (
            SELECT format('%I.%I',n.nspname,c.relname) AS relation_name,
                   row_number() OVER (ORDER BY n.nspname,c.relname)::bigint AS ordinality
              FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE p_relations IS NULL
               AND c.relkind IN ('r','p','m')
               AND NOT c.relispartition
               AND n.nspname NOT IN ('pg_catalog','information_schema','rvbbit')
               AND n.nspname NOT LIKE 'pg_toast%'
               AND n.nspname NOT LIKE 'pg_temp_%'
               AND n.nspname <> ALL(rvbbit._catalog_excluded_schemas())
               AND (p_schemas IS NULL OR n.nspname=ANY(p_schemas))
               AND has_table_privilege(c.oid,'SELECT')
        )
        SELECT relation_name
          FROM (
              SELECT * FROM explicit
              UNION ALL
              SELECT * FROM discovered
          ) candidates
         ORDER BY ordinality
         LIMIT p_max_relations
    LOOP
        v_relation := rvbbit._safe_regclass(v_candidate.relation_name);
        IF v_relation IS NULL THEN
            v_errors := v_errors+1;
            v_error_messages := v_error_messages
                || ('not found or not visible: '||v_candidate.relation_name);
            INSERT INTO rvbbit.business_topology_excavation_sources (
                run_id,relation_name,status,error
            ) VALUES (
                v_run_id,v_candidate.relation_name,'skipped',
                'relation not found or not visible'
            );
            CONTINUE;
        END IF;

        BEGIN
            v_result := rvbbit.business_topology_excavate_relation(
                v_relation,p_sample_rows,p_enqueue,p_force
            );
            v_sources := v_sources+1;
            v_changed := v_changed
                + CASE WHEN coalesce((v_result->>'changed')::boolean,false)
                       THEN 1 ELSE 0 END;
            v_populations := v_populations
                + coalesce((v_result->>'populations_seen')::integer,0);
            v_profiles := v_profiles
                + coalesce((v_result->>'profiles_created')::integer,0);
            v_jobs := v_jobs
                + coalesce((v_result->>'jobs_enqueued')::integer,0);

            INSERT INTO rvbbit.business_topology_excavation_sources (
                run_id,source_id,relation_name,status,changed,
                populations_seen,profiles_created,jobs_enqueued
            ) VALUES (
                v_run_id,(v_result->>'source_id')::uuid,
                v_candidate.relation_name,'complete',
                coalesce((v_result->>'changed')::boolean,false),
                coalesce((v_result->>'populations_seen')::integer,0),
                coalesce((v_result->>'profiles_created')::integer,0),
                coalesce((v_result->>'jobs_enqueued')::integer,0)
            );
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors+1;
            v_error_messages := v_error_messages
                || (v_candidate.relation_name||': '||SQLERRM);
            INSERT INTO rvbbit.business_topology_excavation_sources (
                run_id,relation_name,status,error
            ) VALUES (
                v_run_id,v_candidate.relation_name,'failed',left(SQLERRM,2000)
            );
        END;
    END LOOP;

    IF p_enqueue THEN
        BEGIN
            v_result := rvbbit.business_topology_queue_correspondence_candidates(
                2,20,least(5000,greatest(100,p_max_relations*50))
            );
            v_correspondence_jobs := coalesce(
                (v_result->>'enqueued')::integer,0
            );
            v_jobs := v_jobs+v_correspondence_jobs;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors+1;
            v_error_messages := v_error_messages
                || ('correspondence queue: '||SQLERRM);
        END;
    END IF;

    UPDATE rvbbit.business_topology_excavation_runs
       SET status=CASE
               WHEN v_errors=0 THEN 'complete'
               WHEN v_sources>0 THEN 'partial'
               ELSE 'failed'
           END,
           sources_seen=v_sources,
           sources_changed=v_changed,
           populations_seen=v_populations,
           profiles_created=v_profiles,
           jobs_enqueued=v_jobs,
           errors=v_errors,
           error_summary=CASE
               WHEN v_errors=0 THEN NULL
               ELSE left(array_to_string(v_error_messages,E'\n'),6000)
           END,
           completed_at=clock_timestamp()
     WHERE run_id=v_run_id;

    RETURN jsonb_build_object(
        'run_id',v_run_id,
        'status',CASE
            WHEN v_errors=0 THEN 'complete'
            WHEN v_sources>0 THEN 'partial'
            ELSE 'failed'
        END,
        'sources_seen',v_sources,
        'sources_changed',v_changed,
        'populations_seen',v_populations,
        'profiles_created',v_profiles,
        'jobs_enqueued',v_jobs,
        'correspondence_jobs_enqueued',v_correspondence_jobs,
        'errors',v_errors
    );
END
$fn$;

COMMENT ON FUNCTION rvbbit.business_topology_excavation_run(text[],text[],integer,integer,boolean,boolean) IS
    'Bounded inventory/profile sweep. With no explicit relations, scans caller-readable base/materialized relations outside system and configured excluded schemas.';

-- Local blocking: an inverted salted-fingerprint index finds plausible field
-- correspondences without an O(N^2) comparison.  Values appearing in too many
-- populations are ignored as generic codes/statuses.  These are candidates,
-- not joins and not same-entity assertions.
CREATE OR REPLACE FUNCTION rvbbit.business_topology_overlap_candidates(
    p_min_shared integer DEFAULT 2,
    p_max_fanout integer DEFAULT 20,
    p_limit integer DEFAULT 1000
) RETURNS TABLE (
    left_population_id uuid,
    right_population_id uuid,
    left_name text,
    right_name text,
    shared_fingerprints bigint,
    left_fingerprints bigint,
    right_fingerprints bigint,
    jaccard double precision,
    containment double precision,
    name_token_overlap double precision
)
LANGUAGE sql STABLE
AS $fn$
    WITH current_values AS (
        SELECT p.population_id,p.display_name,p.current_profile_id,
               coalesce(
                   ps.model_packet #>> '{field,name}',p.display_name
               ) AS field_name,
               vf.fingerprint
          FROM rvbbit.business_topology_populations p
          JOIN rvbbit.business_topology_profile_snapshots ps
            ON ps.profile_id=p.current_profile_id
          JOIN rvbbit.business_topology_value_fingerprints vf
            ON vf.population_id=p.population_id
           AND vf.profile_id=p.current_profile_id
         WHERE p.status='active'
           AND p.population_kind='field'
    ), useful_fingerprints AS (
        SELECT fingerprint
          FROM current_values
         GROUP BY fingerprint
        HAVING count(DISTINCT population_id) BETWEEN 2 AND greatest(p_max_fanout,2)
    ), population_counts AS (
        SELECT population_id,count(DISTINCT fingerprint)::bigint AS n
          FROM current_values
         GROUP BY population_id
    ), pairs AS (
        SELECT l.population_id AS left_id,
               r.population_id AS right_id,
               min(l.display_name) AS left_name,
               min(r.display_name) AS right_name,
               min(l.field_name) AS left_field_name,
               min(r.field_name) AS right_field_name,
               count(DISTINCT l.fingerprint)::bigint AS shared
          FROM current_values l
          JOIN useful_fingerprints u USING (fingerprint)
          JOIN current_values r
            ON r.fingerprint=l.fingerprint
           AND r.population_id > l.population_id
         GROUP BY l.population_id,r.population_id
        HAVING count(DISTINCT l.fingerprint) >= greatest(p_min_shared,1)
    )
    SELECT pairs.left_id,pairs.right_id,pairs.left_name,pairs.right_name,
           pairs.shared,lc.n,rc.n,
           pairs.shared::float8
               / nullif(lc.n+rc.n-pairs.shared,0)::float8 AS jaccard,
           greatest(
               pairs.shared::float8/nullif(lc.n,0),
               pairs.shared::float8/nullif(rc.n,0)
           ) AS containment,
           rvbbit._business_topology_token_overlap(
               pairs.left_field_name,pairs.right_field_name
           ) AS name_token_overlap
      FROM pairs
      JOIN population_counts lc ON lc.population_id=pairs.left_id
      JOIN population_counts rc ON rc.population_id=pairs.right_id
     ORDER BY containment DESC,jaccard DESC,pairs.shared DESC,
              pairs.left_name,pairs.right_name
     LIMIT greatest(p_limit,1)
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_correspondence_packet(
    p_left_population_id uuid,
    p_right_population_id uuid
) RETURNS jsonb
LANGUAGE plpgsql STABLE
AS $fn$
DECLARE
    v_left record;
    v_right record;
    v_shared bigint;
    v_left_count bigint;
    v_right_count bigint;
    v_jaccard double precision;
    v_containment double precision;
BEGIN
    IF p_left_population_id=p_right_population_id THEN
        RAISE EXCEPTION 'correspondence requires two different populations';
    END IF;

    SELECT p.population_id,p.display_name,p.current_profile_id,
           s.model_packet,s.profile_hash
      INTO v_left
      FROM rvbbit.business_topology_populations p
      JOIN rvbbit.business_topology_profile_snapshots s
        ON s.profile_id=p.current_profile_id
     WHERE p.population_id=p_left_population_id
       AND p.status='active';
    SELECT p.population_id,p.display_name,p.current_profile_id,
           s.model_packet,s.profile_hash
      INTO v_right
      FROM rvbbit.business_topology_populations p
      JOIN rvbbit.business_topology_profile_snapshots s
        ON s.profile_id=p.current_profile_id
     WHERE p.population_id=p_right_population_id
       AND p.status='active';
    IF v_left.population_id IS NULL OR v_right.population_id IS NULL THEN
        RAISE EXCEPTION 'both populations require an active current profile';
    END IF;

    SELECT count(DISTINCT l.fingerprint),
           (SELECT count(*)
              FROM rvbbit.business_topology_value_fingerprints x
             WHERE x.profile_id=v_left.current_profile_id),
           (SELECT count(*)
              FROM rvbbit.business_topology_value_fingerprints x
             WHERE x.profile_id=v_right.current_profile_id)
      INTO v_shared,v_left_count,v_right_count
      FROM rvbbit.business_topology_value_fingerprints l
      JOIN rvbbit.business_topology_value_fingerprints r
        ON r.fingerprint=l.fingerprint
     WHERE l.profile_id=v_left.current_profile_id
       AND r.profile_id=v_right.current_profile_id;

    v_shared := coalesce(v_shared,0);
    v_left_count := coalesce(v_left_count,0);
    v_right_count := coalesce(v_right_count,0);
    v_jaccard := CASE
        WHEN v_left_count+v_right_count-v_shared=0 THEN 0
        ELSE v_shared::float8/(v_left_count+v_right_count-v_shared)
    END;
    v_containment := greatest(
        CASE WHEN v_left_count=0 THEN 0
             ELSE v_shared::float8/v_left_count END,
        CASE WHEN v_right_count=0 THEN 0
             ELSE v_shared::float8/v_right_count END
    );

    RETURN jsonb_build_object(
        'schema_version','rvbbit.business-topology.correspondence.v1',
        'privacy',jsonb_build_object(
            'raw_values',false,
            'value_hashes',false,
            'local_overlap_only',true
        ),
        'verdict_contract',jsonb_build_array(
            'same_concept','same_facet','same_instance_key','joinable',
            'attribute_of','event_about','measurement_of','category_of',
            'time_of','geography_of','correlated','unrelated','abstain'
        ),
        'left',v_left.model_packet,
        'right',v_right.model_packet,
        'local_evidence',jsonb_build_object(
            'shared_fingerprints',v_shared,
            'left_fingerprints',v_left_count,
            'right_fingerprints',v_right_count,
            'jaccard',v_jaccard,
            'containment',v_containment,
            'name_token_overlap',rvbbit._business_topology_token_overlap(
                coalesce(
                    v_left.model_packet #>> '{field,name}',v_left.display_name
                ),
                coalesce(
                    v_right.model_packet #>> '{field,name}',v_right.display_name
                )
            )
        ),
        'profile_receipts',jsonb_build_object(
            'left_profile_hash',v_left.profile_hash,
            'right_profile_hash',v_right.profile_hash
        )
    );
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_queue_correspondence_candidates(
    p_min_shared integer DEFAULT 2,
    p_max_fanout integer DEFAULT 20,
    p_limit integer DEFAULT 1000
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_candidate record;
    v_packet jsonb;
    v_hash text;
    v_enqueued integer := 0;
    v_affected integer;
BEGIN
    FOR v_candidate IN
        SELECT *
          FROM rvbbit.business_topology_overlap_candidates(
              p_min_shared,p_max_fanout,p_limit
          )
    LOOP
        v_packet := rvbbit.business_topology_correspondence_packet(
            v_candidate.left_population_id,
            v_candidate.right_population_id
        );
        v_hash := md5(v_packet::text);

        INSERT INTO rvbbit.business_topology_inference_jobs (
            task_kind,population_id,peer_population_id,input_hash,input_packet
        ) VALUES (
            'correspondence',v_candidate.left_population_id,
            v_candidate.right_population_id,v_hash,v_packet
        )
        ON CONFLICT (task_kind,input_hash) DO NOTHING;
        GET DIAGNOSTICS v_affected=ROW_COUNT;
        v_enqueued := v_enqueued+v_affected;
    END LOOP;

    RETURN jsonb_build_object(
        'task_kind','correspondence',
        'enqueued',v_enqueued,
        'candidate_limit',p_limit,
        'packet_version','rvbbit.business-topology.correspondence.v1'
    );
END
$fn$;

-- Transactional worker seam for Hutch/Clover.  Cheap specialists should claim
-- these in batches; model state remains outside Postgres while every verdict
-- returns with a model/version receipt.
CREATE OR REPLACE FUNCTION rvbbit.business_topology_claim_inference_jobs(
    p_worker text,
    p_task_kinds text[] DEFAULT NULL,
    p_limit integer DEFAULT 32,
    p_lease_seconds integer DEFAULT 900
) RETURNS TABLE (
    job_id uuid,
    task_kind text,
    population_id uuid,
    peer_population_id uuid,
    input_hash text,
    input_packet jsonb,
    attempt integer
)
LANGUAGE plpgsql VOLATILE
AS $fn$
BEGIN
    IF nullif(btrim(p_worker),'') IS NULL THEN
        RAISE EXCEPTION 'worker name is required';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION 'claim limit must be between 1 and 256';
    END IF;
    IF p_lease_seconds NOT BETWEEN 30 AND 86400 THEN
        RAISE EXCEPTION 'lease_seconds must be between 30 and 86400';
    END IF;

    -- Reclaim abandoned work without touching a genuinely slow current lease.
    UPDATE rvbbit.business_topology_inference_jobs j
       SET status='pending',claimed_by=NULL,claimed_at=NULL,
           not_before=now(),error='worker lease expired'
     WHERE j.status='running'
       AND j.claimed_at < now()-make_interval(secs=>p_lease_seconds);

    RETURN QUERY
    WITH candidates AS (
        SELECT j.job_id
          FROM rvbbit.business_topology_inference_jobs j
         WHERE j.status IN ('pending','failed')
           AND j.not_before <= now()
           AND (p_task_kinds IS NULL OR j.task_kind=ANY(p_task_kinds))
           AND j.attempts < 8
         ORDER BY j.not_before,j.created_at
         FOR UPDATE SKIP LOCKED
         LIMIT p_limit
    )
    UPDATE rvbbit.business_topology_inference_jobs j
       SET status='running',
           attempts=j.attempts+1,
           claimed_by=btrim(p_worker),
           claimed_at=now(),
           error=NULL
      FROM candidates c
     WHERE j.job_id=c.job_id
    RETURNING j.job_id,j.task_kind,j.population_id,j.peer_population_id,
              j.input_hash,j.input_packet,j.attempts;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_complete_inference_job(
    p_job_id uuid,
    p_succeeded boolean,
    p_model_name text DEFAULT NULL,
    p_model_version text DEFAULT NULL,
    p_result jsonb DEFAULT NULL,
    p_error text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_job rvbbit.business_topology_inference_jobs%ROWTYPE;
BEGIN
    SELECT * INTO v_job
      FROM rvbbit.business_topology_inference_jobs
     WHERE job_id=p_job_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'business topology inference job % not found',p_job_id;
    END IF;
    IF v_job.status NOT IN ('running','pending','failed') THEN
        RAISE EXCEPTION 'job % is % and cannot be completed',p_job_id,v_job.status;
    END IF;
    IF p_succeeded AND (
        nullif(btrim(p_model_name),'') IS NULL
        OR nullif(btrim(p_model_version),'') IS NULL
        OR p_result IS NULL
        OR jsonb_typeof(p_result)<>'object'
    ) THEN
        RAISE EXCEPTION
            'successful inference requires model_name, model_version, and an object result';
    END IF;

    UPDATE rvbbit.business_topology_inference_jobs
       SET status=CASE WHEN p_succeeded THEN 'complete' ELSE 'failed' END,
           model_name=coalesce(nullif(btrim(p_model_name),''),model_name),
           model_version=coalesce(nullif(btrim(p_model_version),''),model_version),
           result=CASE WHEN p_succeeded THEN p_result ELSE result END,
           error=CASE WHEN p_succeeded THEN NULL
                      ELSE left(coalesce(nullif(btrim(p_error),''),'inference failed'),4000)
                 END,
           not_before=CASE WHEN p_succeeded THEN not_before
                           ELSE now()+make_interval(
                               secs=>least(3600,15*(2^least(attempts,8))::integer)
                           ) END,
           completed_at=CASE WHEN p_succeeded THEN now() ELSE NULL END
     WHERE job_id=p_job_id;

    RETURN jsonb_build_object(
        'job_id',p_job_id,
        'status',CASE WHEN p_succeeded THEN 'complete' ELSE 'failed' END,
        'attempts',v_job.attempts,
        'model_name',p_model_name,
        'model_version',p_model_version
    );
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_propose(
    p_proposal_kind text,
    p_payload jsonb,
    p_confidence double precision,
    p_inference_kind text DEFAULT 'model',
    p_source_job_id uuid DEFAULT NULL,
    p_model_name text DEFAULT NULL,
    p_model_version text DEFAULT NULL,
    p_proposal_key text DEFAULT NULL,
    p_supersedes uuid DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_key text;
    v_id uuid;
BEGIN
    IF p_proposal_kind NOT IN (
        'population','node','binding','edge','identity_rule','authority',
        'hierarchy'
    ) THEN
        RAISE EXCEPTION 'unsupported business topology proposal kind %',p_proposal_kind;
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' THEN
        RAISE EXCEPTION 'proposal payload must be a JSON object';
    END IF;
    IF p_confidence NOT BETWEEN 0 AND 1 THEN
        RAISE EXCEPTION 'proposal confidence must be between 0 and 1';
    END IF;
    v_key := coalesce(
        nullif(btrim(p_proposal_key),''),
        p_proposal_kind||':'||md5(p_payload::text)
    );

    INSERT INTO rvbbit.business_topology_proposals (
        proposal_key,proposal_kind,payload,confidence,source_job_id,
        inference_kind,model_name,model_version,supersedes
    ) VALUES (
        v_key,p_proposal_kind,p_payload,p_confidence,p_source_job_id,
        coalesce(nullif(btrim(p_inference_kind),''),'model'),
        nullif(btrim(p_model_name),''),nullif(btrim(p_model_version),''),
        p_supersedes
    )
    ON CONFLICT (proposal_key) DO UPDATE SET
        confidence=EXCLUDED.confidence,
        payload=EXCLUDED.payload,
        source_job_id=coalesce(EXCLUDED.source_job_id,
                               rvbbit.business_topology_proposals.source_job_id),
        model_name=coalesce(EXCLUDED.model_name,
                            rvbbit.business_topology_proposals.model_name),
        model_version=coalesce(EXCLUDED.model_version,
                               rvbbit.business_topology_proposals.model_version)
    WHERE rvbbit.business_topology_proposals.status='proposed'
    RETURNING proposal_id INTO v_id;

    IF v_id IS NULL THEN
        SELECT proposal_id INTO v_id
          FROM rvbbit.business_topology_proposals
         WHERE proposal_key=v_key;
    END IF;
    RETURN v_id;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_add_proposal_evidence(
    p_proposal_id uuid,
    p_evidence_kind text,
    p_population_id uuid DEFAULT NULL,
    p_peer_population_id uuid DEFAULT NULL,
    p_score double precision DEFAULT NULL,
    p_weight double precision DEFAULT 1,
    p_source_ref jsonb DEFAULT '{}'::jsonb,
    p_details jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO rvbbit.business_topology_proposal_evidence (
        proposal_id,evidence_kind,population_id,peer_population_id,
        score,weight,source_ref,details
    ) VALUES (
        p_proposal_id,p_evidence_kind,p_population_id,p_peer_population_id,
        p_score,p_weight,coalesce(p_source_ref,'{}'::jsonb),
        coalesce(p_details,'{}'::jsonb)
    )
    RETURNING evidence_id INTO v_id;
    RETURN v_id;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._business_topology_node_key(
    p_node_kind text,
    p_name text,
    p_parent_node_id uuid DEFAULT NULL
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT lower(coalesce(p_node_kind,'object')) || ':'
        || md5(
            lower(btrim(coalesce(p_name,''))) || chr(31)
            || coalesce(p_parent_node_id::text,'root')
        )
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_review_proposal(
    p_proposal_id uuid,
    p_decision text,
    p_reason text DEFAULT NULL,
    p_reviewer text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_proposal rvbbit.business_topology_proposals%ROWTYPE;
    v_reviewer text := coalesce(nullif(btrim(p_reviewer),''),current_user);
    v_materialized jsonb;
    v_node_id uuid;
    v_edge_id uuid;
    v_node_kind text;
    v_name text;
    v_parent uuid;
    v_population uuid;
    v_source_id uuid;
    v_population_kind text;
    v_selector jsonb;
    v_subject uuid;
    v_object uuid;
    v_role text;
    v_authority text;
    v_predicate text;
BEGIN
    IF p_decision NOT IN ('accepted','rejected','withdrawn') THEN
        RAISE EXCEPTION 'decision must be accepted, rejected, or withdrawn';
    END IF;
    SELECT * INTO v_proposal
      FROM rvbbit.business_topology_proposals
     WHERE proposal_id=p_proposal_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'business topology proposal % not found',p_proposal_id;
    END IF;
    IF v_proposal.status<>'proposed' THEN
        RAISE EXCEPTION 'proposal % is already %',p_proposal_id,v_proposal.status;
    END IF;

    IF p_decision='accepted' THEN
        CASE v_proposal.proposal_kind
            WHEN 'population' THEN
                v_source_id := nullif(
                    v_proposal.payload->>'source_id',''
                )::uuid;
                v_population_kind := nullif(
                    v_proposal.payload->>'population_kind',''
                );
                v_selector := v_proposal.payload->'selector';
                IF v_source_id IS NULL
                   OR v_population_kind IS NULL
                   OR v_population_kind NOT IN (
                       'composite','slice','event_stream','mention_set',
                       'query_projection'
                   )
                   OR v_selector IS NULL
                   OR jsonb_typeof(v_selector)<>'object' THEN
                    RAISE EXCEPTION
                        'population proposal requires source_id, a derived population_kind, and an object selector';
                END IF;
                IF v_selector ? 'sql' OR v_selector ? 'where_sql' THEN
                    RAISE EXCEPTION
                        'population selectors are declarative and may not contain executable SQL';
                END IF;
                INSERT INTO rvbbit.business_topology_populations (
                    source_id,population_key,population_kind,selector,
                    display_name,status,created_by,updated_at
                ) VALUES (
                    v_source_id,
                    coalesce(
                        nullif(v_proposal.payload->>'population_key',''),
                        'inferred:'||v_source_id::text||':'
                        ||md5(v_population_kind||chr(31)||v_selector::text)
                    ),
                    v_population_kind,v_selector,
                    coalesce(
                        nullif(v_proposal.payload->>'display_name',''),
                        v_population_kind||' population'
                    ),
                    'active',v_reviewer,now()
                )
                ON CONFLICT (population_key) DO UPDATE SET
                    selector=EXCLUDED.selector,
                    display_name=EXCLUDED.display_name,
                    status='active',updated_at=now()
                RETURNING population_id INTO v_population;
                v_materialized := jsonb_build_object(
                    'kind','population','population_id',v_population
                );

            WHEN 'node' THEN
                v_node_kind := coalesce(
                    nullif(v_proposal.payload->>'node_kind',''),'object'
                );
                v_name := nullif(btrim(v_proposal.payload->>'name'),'');
                v_parent := nullif(v_proposal.payload->>'parent_node_id','')::uuid;
                IF v_name IS NULL THEN
                    RAISE EXCEPTION 'node proposal requires payload.name';
                END IF;
                INSERT INTO rvbbit.business_topology_nodes (
                    node_key,node_kind,name,description,parent_node_id,
                    confidence,properties,source_proposal_id,created_by
                ) VALUES (
                    coalesce(
                        nullif(v_proposal.payload->>'node_key',''),
                        rvbbit._business_topology_node_key(
                            v_node_kind,v_name,v_parent
                        )
                    ),
                    v_node_kind,v_name,v_proposal.payload->>'description',
                    v_parent,v_proposal.confidence,
                    coalesce(v_proposal.payload->'properties','{}'::jsonb),
                    p_proposal_id,v_reviewer
                )
                ON CONFLICT (node_key) DO UPDATE SET
                    description=coalesce(EXCLUDED.description,
                                         rvbbit.business_topology_nodes.description),
                    confidence=greatest(
                        rvbbit.business_topology_nodes.confidence,
                        EXCLUDED.confidence
                    ),
                    properties=rvbbit.business_topology_nodes.properties
                               || EXCLUDED.properties,
                    status='active',updated_at=now()
                RETURNING node_id INTO v_node_id;
                v_materialized := jsonb_build_object(
                    'kind','node','node_id',v_node_id
                );

            WHEN 'binding' THEN
                v_node_id := nullif(v_proposal.payload->>'node_id','')::uuid;
                v_population := nullif(
                    v_proposal.payload->>'population_id',''
                )::uuid;
                v_role := nullif(v_proposal.payload->>'binding_role','');
                v_authority := coalesce(
                    nullif(v_proposal.payload->>'authority',''),'unknown'
                );
                IF v_node_id IS NULL OR v_population IS NULL OR v_role IS NULL THEN
                    RAISE EXCEPTION
                        'binding proposal requires node_id, population_id, and binding_role';
                END IF;
                INSERT INTO rvbbit.business_topology_bindings (
                    node_id,population_id,binding_role,authority,confidence,
                    evidence,source_proposal_id,accepted_by
                ) VALUES (
                    v_node_id,v_population,v_role,v_authority,
                    v_proposal.confidence,
                    coalesce(v_proposal.payload->'evidence','{}'::jsonb),
                    p_proposal_id,v_reviewer
                )
                ON CONFLICT (node_id,population_id,binding_role) DO UPDATE SET
                    authority=EXCLUDED.authority,
                    confidence=EXCLUDED.confidence,
                    evidence=rvbbit.business_topology_bindings.evidence
                             || EXCLUDED.evidence,
                    source_proposal_id=EXCLUDED.source_proposal_id,
                    accepted_by=EXCLUDED.accepted_by,
                    updated_at=now();
                v_materialized := jsonb_build_object(
                    'kind','binding','node_id',v_node_id,
                    'population_id',v_population,'binding_role',v_role
                );

            WHEN 'edge' THEN
                v_subject := nullif(
                    v_proposal.payload->>'subject_node_id',''
                )::uuid;
                v_object := nullif(
                    v_proposal.payload->>'object_node_id',''
                )::uuid;
                v_predicate := nullif(
                    btrim(v_proposal.payload->>'predicate'),'');
                IF v_subject IS NULL OR v_object IS NULL OR v_predicate IS NULL THEN
                    RAISE EXCEPTION
                        'edge proposal requires subject_node_id, predicate, and object_node_id';
                END IF;
                INSERT INTO rvbbit.business_topology_edges (
                    subject_node_id,predicate,object_node_id,confidence,
                    evidence,source_proposal_id,accepted_by
                ) VALUES (
                    v_subject,v_predicate,v_object,v_proposal.confidence,
                    coalesce(v_proposal.payload->'evidence','{}'::jsonb),
                    p_proposal_id,v_reviewer
                )
                ON CONFLICT (subject_node_id,predicate,object_node_id)
                DO UPDATE SET
                    confidence=EXCLUDED.confidence,
                    evidence=rvbbit.business_topology_edges.evidence
                             || EXCLUDED.evidence,
                    source_proposal_id=EXCLUDED.source_proposal_id,
                    accepted_by=EXCLUDED.accepted_by,
                    updated_at=now()
                RETURNING edge_id INTO v_edge_id;
                v_materialized := jsonb_build_object(
                    'kind','edge','edge_id',v_edge_id
                );

            WHEN 'hierarchy' THEN
                v_node_id := nullif(v_proposal.payload->>'node_id','')::uuid;
                v_parent := nullif(
                    v_proposal.payload->>'parent_node_id',''
                )::uuid;
                IF v_node_id IS NULL THEN
                    RAISE EXCEPTION 'hierarchy proposal requires node_id';
                END IF;
                IF v_node_id=v_parent THEN
                    RAISE EXCEPTION 'a topology node cannot parent itself';
                END IF;
                IF v_parent IS NOT NULL AND EXISTS (
                    WITH RECURSIVE descendants AS (
                        SELECT node_id FROM rvbbit.business_topology_nodes
                         WHERE parent_node_id=v_node_id
                        UNION
                        SELECT n.node_id
                          FROM rvbbit.business_topology_nodes n
                          JOIN descendants d ON n.parent_node_id=d.node_id
                    )
                    SELECT 1 FROM descendants WHERE node_id=v_parent
                ) THEN
                    RAISE EXCEPTION 'hierarchy proposal would create a cycle';
                END IF;
                UPDATE rvbbit.business_topology_nodes
                   SET parent_node_id=v_parent,updated_at=now()
                 WHERE node_id=v_node_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'topology node % not found',v_node_id;
                END IF;
                v_materialized := jsonb_build_object(
                    'kind','hierarchy','node_id',v_node_id,
                    'parent_node_id',v_parent
                );

            WHEN 'authority' THEN
                v_node_id := nullif(v_proposal.payload->>'node_id','')::uuid;
                v_population := nullif(
                    v_proposal.payload->>'population_id',''
                )::uuid;
                v_role := nullif(v_proposal.payload->>'binding_role','');
                v_authority := nullif(v_proposal.payload->>'authority','');
                UPDATE rvbbit.business_topology_bindings
                   SET authority=v_authority,
                       source_proposal_id=p_proposal_id,
                       accepted_by=v_reviewer,
                       updated_at=now()
                 WHERE node_id=v_node_id
                   AND population_id=v_population
                   AND binding_role=v_role;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'authority proposal does not identify an existing binding';
                END IF;
                v_materialized := jsonb_build_object(
                    'kind','authority','node_id',v_node_id,
                    'population_id',v_population,'binding_role',v_role
                );

            ELSE
                -- Identity-rule proposals remain governed ledger objects until
                -- the instance-linking contract lands.  Acceptance is still a
                -- useful label and must not silently invent a join predicate.
                v_materialized := jsonb_build_object(
                    'kind',v_proposal.proposal_kind,'ledger_only',true
                );
        END CASE;
    END IF;

    UPDATE rvbbit.business_topology_proposals
       SET status=p_decision,
           reviewed_by=v_reviewer,
           reviewed_at=now(),
           review_reason=nullif(btrim(p_reason),''),
           materialized_ref=v_materialized
     WHERE proposal_id=p_proposal_id;

    INSERT INTO rvbbit.business_topology_proposal_evidence (
        proposal_id,evidence_kind,weight,source_ref,details
    ) VALUES (
        p_proposal_id,'human_decision',1,
        jsonb_build_object('reviewer',v_reviewer),
        jsonb_strip_nulls(jsonb_build_object(
            'decision',p_decision,'reason',nullif(btrim(p_reason),'')
        ))
    );

    RETURN jsonb_build_object(
        'proposal_id',p_proposal_id,
        'status',p_decision,
        'materialized',v_materialized
    );
END
$fn$;

CREATE OR REPLACE VIEW rvbbit.business_topology_skeleton AS
SELECT n.node_id,n.parent_node_id,n.node_kind,n.name,n.description,n.status,
       n.confidence,
       count(DISTINCT b.population_id)::integer AS population_count,
       count(DISTINCT p.source_id)::integer AS source_count,
       coalesce(jsonb_agg(DISTINCT jsonb_build_object(
           'population_id',p.population_id,
           'population',p.display_name,
           'population_kind',p.population_kind,
           'binding_role',b.binding_role,
           'authority',b.authority,
           'confidence',b.confidence,
           'source_id',s.source_id,
           'source_kind',s.source_kind,
           'source',s.locator,
           'fresh_at',s.last_profiled_at
       )) FILTER (WHERE b.population_id IS NOT NULL),'[]'::jsonb) AS bindings,
       n.properties,n.updated_at
  FROM rvbbit.business_topology_nodes n
  LEFT JOIN rvbbit.business_topology_bindings b ON b.node_id=n.node_id
  LEFT JOIN rvbbit.business_topology_populations p
    ON p.population_id=b.population_id
  LEFT JOIN rvbbit.business_topology_sources s ON s.source_id=p.source_id
 GROUP BY n.node_id;

COMMENT ON VIEW rvbbit.business_topology_skeleton IS
    'Readable hierarchical projection of promoted business concepts with source coverage, authority, confidence, and freshness; cross-links remain in business_topology_edges.';
