-- pg_rvbbit 0.44.0 -> 0.45.0
-- KG production hardening: graph namespaces, extraction run/error audit,
-- table ingestion helper, and tunable context depth decay.

ALTER TABLE rvbbit.kg_nodes ADD COLUMN IF NOT EXISTS graph_id text NOT NULL DEFAULT 'default';
ALTER TABLE rvbbit.kg_aliases ADD COLUMN IF NOT EXISTS graph_id text NOT NULL DEFAULT 'default';
ALTER TABLE rvbbit.kg_edges ADD COLUMN IF NOT EXISTS graph_id text NOT NULL DEFAULT 'default';
ALTER TABLE rvbbit.kg_evidence ADD COLUMN IF NOT EXISTS graph_id text NOT NULL DEFAULT 'default';
ALTER TABLE rvbbit.kg_merge_candidates ADD COLUMN IF NOT EXISTS graph_id text NOT NULL DEFAULT 'default';
ALTER TABLE rvbbit.kg_node_merges ADD COLUMN IF NOT EXISTS graph_id text NOT NULL DEFAULT 'default';

UPDATE rvbbit.kg_aliases a SET graph_id = n.graph_id FROM rvbbit.kg_nodes n WHERE a.node_id = n.node_id AND a.graph_id IS DISTINCT FROM n.graph_id;
UPDATE rvbbit.kg_edges e SET graph_id = n.graph_id FROM rvbbit.kg_nodes n WHERE e.subject_node_id = n.node_id AND e.graph_id IS DISTINCT FROM n.graph_id;
UPDATE rvbbit.kg_evidence ev SET graph_id = e.graph_id FROM rvbbit.kg_edges e WHERE ev.edge_id = e.edge_id AND ev.graph_id IS DISTINCT FROM e.graph_id;
UPDATE rvbbit.kg_evidence ev SET graph_id = n.graph_id FROM rvbbit.kg_nodes n WHERE ev.node_id = n.node_id AND ev.edge_id IS NULL AND ev.graph_id IS DISTINCT FROM n.graph_id;

ALTER TABLE rvbbit.kg_nodes DROP CONSTRAINT IF EXISTS kg_nodes_kind_label_unique;
ALTER TABLE rvbbit.kg_nodes DROP CONSTRAINT IF EXISTS kg_nodes_graph_kind_label_unique;
ALTER TABLE rvbbit.kg_nodes ADD CONSTRAINT kg_nodes_graph_kind_label_unique UNIQUE (graph_id, kind, label_norm);
ALTER TABLE rvbbit.kg_aliases DROP CONSTRAINT IF EXISTS kg_aliases_kind_alias_unique;
ALTER TABLE rvbbit.kg_aliases DROP CONSTRAINT IF EXISTS kg_aliases_graph_kind_alias_unique;
ALTER TABLE rvbbit.kg_aliases ADD CONSTRAINT kg_aliases_graph_kind_alias_unique UNIQUE (graph_id, kind, alias_norm);
ALTER TABLE rvbbit.kg_edges DROP CONSTRAINT IF EXISTS kg_edges_unique_fact;
ALTER TABLE rvbbit.kg_edges DROP CONSTRAINT IF EXISTS kg_edges_graph_unique_fact;
ALTER TABLE rvbbit.kg_edges ADD CONSTRAINT kg_edges_graph_unique_fact UNIQUE (graph_id, subject_node_id, predicate_norm, object_node_id);
ALTER TABLE rvbbit.kg_merge_candidates DROP CONSTRAINT IF EXISTS kg_merge_candidates_pair_method_unique;
ALTER TABLE rvbbit.kg_merge_candidates DROP CONSTRAINT IF EXISTS kg_merge_candidates_graph_pair_method_unique;
ALTER TABLE rvbbit.kg_merge_candidates ADD CONSTRAINT kg_merge_candidates_graph_pair_method_unique UNIQUE (graph_id, left_node_id, right_node_id, method);

DROP FUNCTION IF EXISTS rvbbit.kg_assert_alias(bigint,text,double precision,jsonb);
DROP FUNCTION IF EXISTS rvbbit.kg_resolve_node(text,text,text,double precision);
DROP FUNCTION IF EXISTS rvbbit.kg_assert_node(text,text,jsonb,double precision,text,double precision);
DROP FUNCTION IF EXISTS rvbbit.kg_link_evidence(bigint,bigint,regclass,text,text,text,double precision,jsonb,int4range);
DROP FUNCTION IF EXISTS rvbbit.kg_assert_edge(text,text,text,text,text,double precision,jsonb,jsonb,text,double precision);
DROP FUNCTION IF EXISTS rvbbit.kg_suggest_merges(text,double precision,int);
DROP FUNCTION IF EXISTS rvbbit.kg_neighbors(text,text,int,text,text,double precision);
DROP FUNCTION IF EXISTS rvbbit.kg_paths(text,text,text,text,int,text,text,double precision);
DROP FUNCTION IF EXISTS rvbbit.kg_context(text,text,int,int,text,boolean,text,double precision);
DROP FUNCTION IF EXISTS rvbbit.kg_ingest_triples(text,regclass,text,text,text,double precision);

CREATE TABLE IF NOT EXISTS rvbbit.kg_nodes (
    node_id     bigserial PRIMARY KEY,
    graph_id    text NOT NULL DEFAULT 'default',
    kind        text NOT NULL,
    label       text NOT NULL,
    label_norm  text NOT NULL,
    properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence  double precision NOT NULL DEFAULT 1.0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_nodes_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT kg_nodes_graph_kind_label_unique UNIQUE (graph_id, kind, label_norm)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_aliases (
    alias_id    bigserial PRIMARY KEY,
    node_id     bigint NOT NULL REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    graph_id    text NOT NULL DEFAULT 'default',
    kind        text NOT NULL,
    alias       text NOT NULL,
    alias_norm  text NOT NULL,
    confidence  double precision NOT NULL DEFAULT 1.0,
    properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_aliases_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT kg_aliases_graph_kind_alias_unique UNIQUE (graph_id, kind, alias_norm)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_edges (
    edge_id          bigserial PRIMARY KEY,
    graph_id         text NOT NULL DEFAULT 'default',
    subject_node_id  bigint NOT NULL REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    predicate        text NOT NULL,
    predicate_norm   text NOT NULL,
    object_node_id   bigint NOT NULL REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    properties       jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence       double precision NOT NULL DEFAULT 1.0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_edges_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT kg_edges_graph_unique_fact UNIQUE (graph_id, subject_node_id, predicate_norm, object_node_id)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_evidence (
    evidence_id   bigserial PRIMARY KEY,
    graph_id      text NOT NULL DEFAULT 'default',
    edge_id       bigint REFERENCES rvbbit.kg_edges(edge_id) ON DELETE CASCADE,
    node_id       bigint REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    query_id      uuid,
    source_table  regclass,
    source_pk     text,
    source_column text,
    evidence_text text,
    span          int4range,
    confidence    double precision NOT NULL DEFAULT 1.0,
    properties    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_evidence_target_check CHECK (edge_id IS NOT NULL OR node_id IS NOT NULL),
    CONSTRAINT kg_evidence_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_merge_candidates (
    candidate_id   bigserial PRIMARY KEY,
    graph_id       text NOT NULL DEFAULT 'default',
    query_id       uuid,
    left_node_id   bigint NOT NULL,
    right_node_id  bigint NOT NULL,
    kind           text NOT NULL,
    score          double precision NOT NULL,
    method         text NOT NULL DEFAULT 'label_similarity',
    reason         text,
    status         text NOT NULL DEFAULT 'pending',
    properties     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    reviewed_at    timestamptz,
    CONSTRAINT kg_merge_candidates_order_check CHECK (left_node_id < right_node_id),
    CONSTRAINT kg_merge_candidates_score_check CHECK (score >= 0.0 AND score <= 1.0),
    CONSTRAINT kg_merge_candidates_status_check CHECK (status IN ('pending', 'accepted', 'rejected', 'superseded')),
    CONSTRAINT kg_merge_candidates_graph_pair_method_unique UNIQUE (graph_id, left_node_id, right_node_id, method)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_node_merges (
    merge_id          bigserial PRIMARY KEY,
    graph_id          text NOT NULL DEFAULT 'default',
    query_id          uuid,
    candidate_id      bigint REFERENCES rvbbit.kg_merge_candidates(candidate_id) ON DELETE SET NULL,
    winner_node_id    bigint REFERENCES rvbbit.kg_nodes(node_id) ON DELETE SET NULL,
    loser_node_id     bigint NOT NULL,
    loser_kind        text NOT NULL,
    loser_label       text NOT NULL,
    loser_label_norm  text NOT NULL,
    loser_properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
    properties        jsonb NOT NULL DEFAULT '{}'::jsonb,
    merged_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kg_aliases_node_idx ON rvbbit.kg_aliases(node_id);
CREATE INDEX IF NOT EXISTS kg_nodes_graph_kind_idx ON rvbbit.kg_nodes(graph_id, kind);
CREATE INDEX IF NOT EXISTS kg_aliases_graph_kind_idx ON rvbbit.kg_aliases(graph_id, kind);
CREATE INDEX IF NOT EXISTS kg_edges_subject_idx ON rvbbit.kg_edges(subject_node_id);
CREATE INDEX IF NOT EXISTS kg_edges_object_idx ON rvbbit.kg_edges(object_node_id);
CREATE INDEX IF NOT EXISTS kg_edges_graph_subject_idx ON rvbbit.kg_edges(graph_id, subject_node_id);
CREATE INDEX IF NOT EXISTS kg_edges_graph_object_idx ON rvbbit.kg_edges(graph_id, object_node_id);
CREATE INDEX IF NOT EXISTS kg_edges_predicate_idx ON rvbbit.kg_edges(predicate_norm);
CREATE INDEX IF NOT EXISTS kg_edges_graph_predicate_idx ON rvbbit.kg_edges(graph_id, predicate_norm);
CREATE INDEX IF NOT EXISTS kg_evidence_edge_idx ON rvbbit.kg_evidence(edge_id);
CREATE INDEX IF NOT EXISTS kg_evidence_node_idx ON rvbbit.kg_evidence(node_id);
CREATE INDEX IF NOT EXISTS kg_evidence_graph_edge_idx ON rvbbit.kg_evidence(graph_id, edge_id);
CREATE INDEX IF NOT EXISTS kg_evidence_graph_node_idx ON rvbbit.kg_evidence(graph_id, node_id);
CREATE INDEX IF NOT EXISTS kg_evidence_query_id_idx ON rvbbit.kg_evidence(query_id);
CREATE INDEX IF NOT EXISTS kg_evidence_source_idx
    ON rvbbit.kg_evidence(source_table, source_pk)
    WHERE source_table IS NOT NULL OR source_pk IS NOT NULL;
CREATE INDEX IF NOT EXISTS kg_evidence_graph_source_idx
    ON rvbbit.kg_evidence(graph_id, source_table, source_pk, source_column)
    WHERE source_table IS NOT NULL OR source_pk IS NOT NULL;
CREATE INDEX IF NOT EXISTS kg_merge_candidates_left_idx ON rvbbit.kg_merge_candidates(left_node_id);
CREATE INDEX IF NOT EXISTS kg_merge_candidates_right_idx ON rvbbit.kg_merge_candidates(right_node_id);
CREATE INDEX IF NOT EXISTS kg_merge_candidates_status_idx ON rvbbit.kg_merge_candidates(status, score DESC);
CREATE INDEX IF NOT EXISTS kg_merge_candidates_query_id_idx ON rvbbit.kg_merge_candidates(query_id);
CREATE INDEX IF NOT EXISTS kg_node_merges_winner_idx ON rvbbit.kg_node_merges(winner_node_id);
CREATE INDEX IF NOT EXISTS kg_node_merges_loser_idx ON rvbbit.kg_node_merges(loser_node_id);
CREATE INDEX IF NOT EXISTS kg_node_merges_query_id_idx ON rvbbit.kg_node_merges(query_id);

CREATE TABLE IF NOT EXISTS rvbbit.kg_extraction_runs (
    run_id           bigserial PRIMARY KEY,
    graph_id         text NOT NULL DEFAULT 'default',
    query_id         uuid,
    source_table     regclass,
    source_column    text,
    focus            text,
    status           text NOT NULL DEFAULT 'running',
    rows_seen        bigint NOT NULL DEFAULT 0,
    triples_inserted bigint NOT NULL DEFAULT 0,
    errors           bigint NOT NULL DEFAULT 0,
    properties       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    CONSTRAINT kg_extraction_runs_status_check CHECK (status IN ('running', 'ok', 'partial', 'failed'))
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_extraction_errors (
    error_id      bigserial PRIMARY KEY,
    run_id        bigint REFERENCES rvbbit.kg_extraction_runs(run_id) ON DELETE CASCADE,
    graph_id      text NOT NULL DEFAULT 'default',
    query_id      uuid,
    source_table  regclass,
    source_pk     text,
    source_column text,
    input_text    text,
    error         text NOT NULL,
    properties    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kg_extraction_runs_graph_idx ON rvbbit.kg_extraction_runs(graph_id, created_at DESC);
CREATE INDEX IF NOT EXISTS kg_extraction_runs_query_id_idx ON rvbbit.kg_extraction_runs(query_id);
CREATE INDEX IF NOT EXISTS kg_extraction_errors_run_idx ON rvbbit.kg_extraction_errors(run_id);
CREATE INDEX IF NOT EXISTS kg_extraction_errors_query_id_idx ON rvbbit.kg_extraction_errors(query_id);

CREATE OR REPLACE FUNCTION rvbbit.kg_touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS kg_nodes_touch_updated_at ON rvbbit.kg_nodes;
CREATE TRIGGER kg_nodes_touch_updated_at
    BEFORE UPDATE ON rvbbit.kg_nodes
    FOR EACH ROW EXECUTE FUNCTION rvbbit.kg_touch_updated_at();

DROP TRIGGER IF EXISTS kg_edges_touch_updated_at ON rvbbit.kg_edges;
CREATE TRIGGER kg_edges_touch_updated_at
    BEFORE UPDATE ON rvbbit.kg_edges
    FOR EACH ROW EXECUTE FUNCTION rvbbit.kg_touch_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.kg_normalize_label(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT regexp_replace(lower(btrim(value)), '\s+', ' ', 'g')
$$;

CREATE OR REPLACE FUNCTION rvbbit.kg_normalize_predicate(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT regexp_replace(lower(btrim(value)), '\s+', '_', 'g')
$$;

CREATE OR REPLACE FUNCTION rvbbit.kg_normalize_graph(value text DEFAULT NULL)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(NULLIF(regexp_replace(lower(btrim(value)), '\s+', '_', 'g'), ''), 'default')
$$;

CREATE OR REPLACE FUNCTION rvbbit.kg_label_similarity(left_label text, right_label text)
RETURNS double precision
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    left_norm text := rvbbit.kg_normalize_label(left_label);
    right_norm text := rvbbit.kg_normalize_label(right_label);
    left_tokens text[];
    right_tokens text[];
    lt text;
    rt text;
    token_matches int := 0;
    denom int := 0;
    token_score double precision := 0.0;
    containment_score double precision := 0.0;
BEGIN
    IF left_norm = '' OR right_norm = '' THEN
        RETURN 0.0;
    END IF;
    IF left_norm = right_norm THEN
        RETURN 1.0;
    END IF;

    SELECT COALESCE(array_agg(DISTINCT token), ARRAY[]::text[])
    INTO left_tokens
    FROM regexp_split_to_table(left_norm, '[^[:alnum:]]+') AS t(token)
    WHERE token <> '';

    SELECT COALESCE(array_agg(DISTINCT token), ARRAY[]::text[])
    INTO right_tokens
    FROM regexp_split_to_table(right_norm, '[^[:alnum:]]+') AS t(token)
    WHERE token <> '';

    IF position(left_norm in right_norm) > 0 OR position(right_norm in left_norm) > 0 THEN
        containment_score := 0.86;
    END IF;

    FOREACH lt IN ARRAY left_tokens LOOP
        FOREACH rt IN ARRAY right_tokens LOOP
            IF lt = rt
               OR (
                   length(lt) >= 4
                   AND length(rt) >= 4
                   AND (position(lt in rt) = 1 OR position(rt in lt) = 1)
               ) THEN
                token_matches := token_matches + 1;
                EXIT;
            END IF;
        END LOOP;
    END LOOP;

    denom := greatest(COALESCE(array_length(left_tokens, 1), 0), COALESCE(array_length(right_tokens, 1), 0));
    IF denom > 0 THEN
        token_score := token_matches::double precision / denom::double precision;
    END IF;

    RETURN least(greatest(token_score, containment_score), 0.99);
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_assert_alias(
    target_node_id bigint,
    alias_label text,
    confidence double precision DEFAULT 1.0,
    properties jsonb DEFAULT '{}'::jsonb,
    graph text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    node_kind text;
    node_graph text;
    norm_graph text;
    norm_alias text;
    out_alias_id bigint;
    alias_props jsonb;
BEGIN
    IF alias_label IS NULL OR btrim(alias_label) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_alias: alias_label must be non-empty';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_alias: confidence must be between 0 and 1';
    END IF;

    SELECT kind, graph_id INTO node_kind, node_graph
    FROM rvbbit.kg_nodes
    WHERE node_id = target_node_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_alias: node_id % not found', target_node_id;
    END IF;

    norm_graph := COALESCE(rvbbit.kg_normalize_graph(graph), node_graph);
    IF norm_graph <> node_graph THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_alias: node % is in graph %, not %',
            target_node_id, node_graph, norm_graph;
    END IF;

    norm_alias := rvbbit.kg_normalize_label(alias_label);
    alias_props := COALESCE(properties, '{}'::jsonb);
    INSERT INTO rvbbit.kg_aliases(node_id, graph_id, kind, alias, alias_norm, confidence, properties)
    VALUES (target_node_id, norm_graph, node_kind, btrim(alias_label), norm_alias, confidence, alias_props)
    ON CONFLICT (graph_id, kind, alias_norm) DO UPDATE SET
        node_id = EXCLUDED.node_id,
        alias = EXCLUDED.alias,
        confidence = greatest(rvbbit.kg_aliases.confidence, EXCLUDED.confidence),
        properties = rvbbit.kg_aliases.properties || EXCLUDED.properties
    RETURNING alias_id INTO out_alias_id;

    RETURN out_alias_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_resolve_node(
    node_kind text,
    node_label text,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS TABLE (
    node_id bigint,
    kind text,
    label text,
    score double precision,
    match_method text
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    norm_kind text;
    norm_label text;
    norm_graph text;
BEGIN
    IF node_kind IS NULL OR btrim(node_kind) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_resolve_node: node_kind must be non-empty';
    END IF;
    IF node_label IS NULL OR btrim(node_label) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_resolve_node: node_label must be non-empty';
    END IF;
    IF match_threshold IS NULL THEN
        match_threshold := 0.92;
    END IF;
    IF match_threshold < 0.0 OR match_threshold > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_resolve_node: match_threshold must be between 0 and 1';
    END IF;

    norm_kind := rvbbit.kg_normalize_label(node_kind);
    norm_label := rvbbit.kg_normalize_label(node_label);
    norm_graph := rvbbit.kg_normalize_graph(graph);

    RETURN QUERY
    SELECT n.node_id, n.kind, n.label, 1.0::double precision, 'alias'::text
    FROM rvbbit.kg_aliases a
    JOIN rvbbit.kg_nodes n ON n.node_id = a.node_id
    WHERE a.graph_id = norm_graph
      AND n.graph_id = norm_graph
      AND a.kind = norm_kind
      AND a.alias_norm = norm_label
    ORDER BY a.confidence DESC, n.node_id
    LIMIT 1;

    IF FOUND THEN
        RETURN;
    END IF;

    IF match_threshold > 0.0 THEN
        RETURN QUERY
        SELECT n.node_id, n.kind, n.label, s.score, 'embedding'::text
        FROM rvbbit.kg_nodes n
        CROSS JOIN LATERAL (
            SELECT rvbbit.similarity(node_label, n.label, specialist) AS score
        ) s
        WHERE n.graph_id = norm_graph
          AND n.kind = norm_kind
          AND s.score >= match_threshold
        ORDER BY s.score DESC, n.node_id
        LIMIT 10;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_assert_node(
    node_kind text,
    node_label text,
    properties jsonb DEFAULT '{}'::jsonb,
    confidence double precision DEFAULT 1.0,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    norm_kind text;
    norm_label text;
    norm_graph text;
    resolved_id bigint;
    out_node_id bigint;
    node_props jsonb;
BEGIN
    IF node_kind IS NULL OR btrim(node_kind) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_node: node_kind must be non-empty';
    END IF;
    IF node_label IS NULL OR btrim(node_label) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_node: node_label must be non-empty';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_node: confidence must be between 0 and 1';
    END IF;

    norm_kind := rvbbit.kg_normalize_label(node_kind);
    norm_label := rvbbit.kg_normalize_label(node_label);
    norm_graph := rvbbit.kg_normalize_graph(graph);
    node_props := COALESCE(properties, '{}'::jsonb);

    SELECT r.node_id INTO resolved_id
    FROM rvbbit.kg_resolve_node(norm_kind, node_label, specialist, match_threshold, norm_graph) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;

    IF resolved_id IS NOT NULL THEN
        UPDATE rvbbit.kg_nodes
        SET properties = rvbbit.kg_nodes.properties || node_props,
            confidence = greatest(rvbbit.kg_nodes.confidence, kg_assert_node.confidence)
        WHERE node_id = resolved_id;

        PERFORM rvbbit.kg_assert_alias(resolved_id, node_label, confidence, '{}'::jsonb, norm_graph);
        RETURN resolved_id;
    END IF;

    INSERT INTO rvbbit.kg_nodes(graph_id, kind, label, label_norm, properties, confidence)
    VALUES (norm_graph, norm_kind, btrim(node_label), norm_label, node_props, confidence)
    ON CONFLICT (graph_id, kind, label_norm) DO UPDATE SET
        label = EXCLUDED.label,
        properties = rvbbit.kg_nodes.properties || EXCLUDED.properties,
        confidence = greatest(rvbbit.kg_nodes.confidence, EXCLUDED.confidence)
    RETURNING node_id INTO out_node_id;

    PERFORM rvbbit.kg_assert_alias(out_node_id, node_label, confidence, '{}'::jsonb, norm_graph);
    RETURN out_node_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_link_evidence(
    target_edge_id bigint DEFAULT NULL,
    target_node_id bigint DEFAULT NULL,
    source_table regclass DEFAULT NULL,
    source_pk text DEFAULT NULL,
    source_column text DEFAULT NULL,
    evidence_text text DEFAULT NULL,
    confidence double precision DEFAULT 1.0,
    properties jsonb DEFAULT '{}'::jsonb,
    span int4range DEFAULT NULL,
    graph text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    out_evidence_id bigint;
    evidence_props jsonb;
    raw_query_id text;
    resolved_query_id uuid;
    edge_graph text;
    node_graph text;
    norm_graph text;
BEGIN
    IF target_edge_id IS NULL AND target_node_id IS NULL THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: target_edge_id or target_node_id is required';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: confidence must be between 0 and 1';
    END IF;

    evidence_props := COALESCE(properties, '{}'::jsonb);
    IF target_edge_id IS NOT NULL THEN
        SELECT graph_id INTO edge_graph FROM rvbbit.kg_edges WHERE edge_id = target_edge_id;
        IF edge_graph IS NULL THEN
            RAISE EXCEPTION 'rvbbit.kg_link_evidence: edge_id % not found', target_edge_id;
        END IF;
    END IF;
    IF target_node_id IS NOT NULL THEN
        SELECT graph_id INTO node_graph FROM rvbbit.kg_nodes WHERE node_id = target_node_id;
        IF node_graph IS NULL THEN
            RAISE EXCEPTION 'rvbbit.kg_link_evidence: node_id % not found', target_node_id;
        END IF;
    END IF;
    IF edge_graph IS NOT NULL AND node_graph IS NOT NULL AND edge_graph <> node_graph THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: edge graph % does not match node graph %',
            edge_graph, node_graph;
    END IF;
    norm_graph := COALESCE(edge_graph, node_graph, rvbbit.kg_normalize_graph(graph));
    IF graph IS NOT NULL AND rvbbit.kg_normalize_graph(graph) <> norm_graph THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: target is in graph %, not %',
            norm_graph, rvbbit.kg_normalize_graph(graph);
    END IF;

    raw_query_id := NULLIF(COALESCE(
        evidence_props->>'query_id',
        evidence_props #>> '{properties,query_id}',
        ''
    ), '');
    IF raw_query_id IS NOT NULL THEN
        BEGIN
            resolved_query_id := raw_query_id::uuid;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'rvbbit.kg_link_evidence: invalid query_id in properties: %', raw_query_id;
        END;
    ELSE
        resolved_query_id := rvbbit.current_query_id();
    END IF;

    INSERT INTO rvbbit.kg_evidence(
        graph_id, edge_id, node_id, query_id, source_table, source_pk, source_column,
        evidence_text, span, confidence, properties
    )
    VALUES (
        norm_graph, target_edge_id, target_node_id, resolved_query_id, source_table, source_pk, source_column,
        evidence_text, span, confidence, evidence_props
    )
    RETURNING evidence_id INTO out_evidence_id;

    RETURN out_evidence_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_assert_edge(
    subject_kind text,
    subject_label text,
    predicate text,
    object_kind text,
    object_label text,
    confidence double precision DEFAULT 1.0,
    evidence jsonb DEFAULT '{}'::jsonb,
    properties jsonb DEFAULT '{}'::jsonb,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    subj_id bigint;
    obj_id bigint;
    norm_pred text;
    out_edge_id bigint;
    evidence_text text;
    edge_props jsonb;
    evidence_doc jsonb;
    norm_graph text;
BEGIN
    IF predicate IS NULL OR btrim(predicate) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_edge: predicate must be non-empty';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_edge: confidence must be between 0 and 1';
    END IF;

    norm_graph := rvbbit.kg_normalize_graph(graph);
    subj_id := rvbbit.kg_assert_node(subject_kind, subject_label, '{}'::jsonb, confidence, specialist, match_threshold, norm_graph);
    obj_id := rvbbit.kg_assert_node(object_kind, object_label, '{}'::jsonb, confidence, specialist, match_threshold, norm_graph);
    norm_pred := rvbbit.kg_normalize_predicate(predicate);
    edge_props := COALESCE(properties, '{}'::jsonb);
    evidence_doc := COALESCE(evidence, '{}'::jsonb);

    INSERT INTO rvbbit.kg_edges(graph_id, subject_node_id, predicate, predicate_norm, object_node_id, properties, confidence)
    VALUES (norm_graph, subj_id, btrim(predicate), norm_pred, obj_id, edge_props, confidence)
    ON CONFLICT (graph_id, subject_node_id, predicate_norm, object_node_id) DO UPDATE SET
        predicate = EXCLUDED.predicate,
        properties = rvbbit.kg_edges.properties || EXCLUDED.properties,
        confidence = greatest(rvbbit.kg_edges.confidence, EXCLUDED.confidence)
    RETURNING edge_id INTO out_edge_id;

    IF evidence_doc <> '{}'::jsonb THEN
        evidence_text := COALESCE(evidence_doc->>'text', evidence_doc->>'evidence_text', evidence_doc->>'quote');
        PERFORM rvbbit.kg_link_evidence(
            target_edge_id => out_edge_id,
            evidence_text => evidence_text,
            confidence => confidence,
            properties => evidence_doc,
            graph => norm_graph
        );
    END IF;

    RETURN out_edge_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_suggest_merges(
    node_kind text DEFAULT NULL,
    threshold double precision DEFAULT 0.86,
    limit_count int DEFAULT 1000,
    graph text DEFAULT NULL
) RETURNS TABLE (
    candidate_id bigint,
    left_node_id bigint,
    left_label text,
    right_node_id bigint,
    right_label text,
    kind text,
    score double precision,
    method text,
    status text,
    reason text
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    norm_kind text;
    norm_graph text := rvbbit.kg_normalize_graph(graph);
    min_score double precision := COALESCE(threshold, 0.86);
    max_rows int := greatest(COALESCE(limit_count, 1000), 1);
    qid uuid := rvbbit.current_query_id();
BEGIN
    IF min_score < 0.0 OR min_score > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_suggest_merges: threshold must be between 0 and 1';
    END IF;
    IF node_kind IS NOT NULL AND btrim(node_kind) <> '' THEN
        norm_kind := rvbbit.kg_normalize_label(node_kind);
    END IF;

    RETURN QUERY
    WITH scored AS (
        SELECT n1.node_id AS left_id,
               n2.node_id AS right_id,
               n1.kind AS node_kind,
               n1.label AS left_name,
               n2.label AS right_name,
               rvbbit.kg_label_similarity(n1.label, n2.label) AS pair_score
        FROM rvbbit.kg_nodes n1
        JOIN rvbbit.kg_nodes n2
          ON n1.graph_id = n2.graph_id
         AND n1.kind = n2.kind
         AND n1.node_id < n2.node_id
        WHERE (norm_kind IS NULL OR n1.kind = norm_kind)
          AND n1.graph_id = norm_graph
          AND NOT EXISTS (
              SELECT 1
              FROM rvbbit.kg_merge_candidates c
              WHERE c.graph_id = norm_graph
                AND c.left_node_id = n1.node_id
                AND c.right_node_id = n2.node_id
                AND c.status IN ('accepted', 'rejected', 'superseded')
          )
    ),
    picked AS (
        SELECT *
        FROM scored
        WHERE pair_score >= min_score
        ORDER BY pair_score DESC, left_id, right_id
        LIMIT max_rows
    ),
    upserted AS (
        INSERT INTO rvbbit.kg_merge_candidates(
            graph_id, query_id, left_node_id, right_node_id, kind, score, method, reason, status, properties
        )
        SELECT norm_graph,
               qid,
               p.left_id,
               p.right_id,
               p.node_kind,
               p.pair_score,
               'label_similarity',
               format('label similarity %s between "%s" and "%s"', round(p.pair_score::numeric, 3), p.left_name, p.right_name),
               'pending',
               jsonb_build_object('left_label', p.left_name, 'right_label', p.right_name)
        FROM picked p
        ON CONFLICT ON CONSTRAINT kg_merge_candidates_graph_pair_method_unique DO UPDATE SET
            query_id = EXCLUDED.query_id,
            score = EXCLUDED.score,
            reason = EXCLUDED.reason,
            properties = rvbbit.kg_merge_candidates.properties || EXCLUDED.properties
        WHERE rvbbit.kg_merge_candidates.status = 'pending'
        RETURNING rvbbit.kg_merge_candidates.*
    )
    SELECT u.candidate_id,
           u.left_node_id,
           ln.label,
           u.right_node_id,
           rn.label,
           u.kind,
           u.score,
           u.method,
           u.status,
           u.reason
    FROM upserted u
    JOIN rvbbit.kg_nodes ln ON ln.node_id = u.left_node_id
    JOIN rvbbit.kg_nodes rn ON rn.node_id = u.right_node_id
    ORDER BY u.score DESC, u.candidate_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_reject_merge(target_candidate_id bigint)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    out_candidate_id bigint;
BEGIN
    UPDATE rvbbit.kg_merge_candidates
    SET status = 'rejected',
        reviewed_at = now(),
        query_id = COALESCE(query_id, rvbbit.current_query_id())
    WHERE candidate_id = target_candidate_id
      AND status = 'pending'
    RETURNING candidate_id INTO out_candidate_id;

    IF out_candidate_id IS NULL THEN
        RAISE EXCEPTION 'rvbbit.kg_reject_merge: pending candidate % not found', target_candidate_id;
    END IF;

    RETURN out_candidate_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_merge_nodes(
    winner_node_id bigint,
    loser_node_id bigint,
    merge_candidate_id bigint DEFAULT NULL,
    merge_properties jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    winner rvbbit.kg_nodes%ROWTYPE;
    loser rvbbit.kg_nodes%ROWTYPE;
    edge_row rvbbit.kg_edges%ROWTYPE;
    new_subject_id bigint;
    new_object_id bigint;
    existing_edge_id bigint;
    out_merge_id bigint;
    qid uuid := rvbbit.current_query_id();
    props jsonb := COALESCE(merge_properties, '{}'::jsonb);
BEGIN
    IF winner_node_id IS NULL OR loser_node_id IS NULL THEN
        RAISE EXCEPTION 'rvbbit.kg_merge_nodes: winner_node_id and loser_node_id are required';
    END IF;
    IF winner_node_id = loser_node_id THEN
        RAISE EXCEPTION 'rvbbit.kg_merge_nodes: winner and loser must be different nodes';
    END IF;

    SELECT * INTO winner
    FROM rvbbit.kg_nodes
    WHERE node_id = winner_node_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.kg_merge_nodes: winner node % not found', winner_node_id;
    END IF;

    SELECT * INTO loser
    FROM rvbbit.kg_nodes
    WHERE node_id = loser_node_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.kg_merge_nodes: loser node % not found', loser_node_id;
    END IF;

    IF winner.kind <> loser.kind THEN
        RAISE EXCEPTION 'rvbbit.kg_merge_nodes: cannot merge different kinds (% vs %)', winner.kind, loser.kind;
    END IF;
    IF winner.graph_id <> loser.graph_id THEN
        RAISE EXCEPTION 'rvbbit.kg_merge_nodes: cannot merge nodes from different graphs (% vs %)', winner.graph_id, loser.graph_id;
    END IF;

    INSERT INTO rvbbit.kg_node_merges(
        graph_id, query_id, candidate_id, winner_node_id, loser_node_id,
        loser_kind, loser_label, loser_label_norm, loser_properties, properties
    )
    VALUES (
        winner.graph_id, qid, merge_candidate_id, winner.node_id, loser.node_id,
        loser.kind, loser.label, loser.label_norm, loser.properties, props
    )
    RETURNING merge_id INTO out_merge_id;

    UPDATE rvbbit.kg_nodes
    SET properties = loser.properties || winner.properties || props,
        confidence = greatest(winner.confidence, loser.confidence)
    WHERE node_id = winner.node_id;

    INSERT INTO rvbbit.kg_aliases(node_id, graph_id, kind, alias, alias_norm, confidence, properties)
    SELECT winner.node_id,
           graph_id,
           kind,
           alias,
           alias_norm,
           confidence,
           properties || jsonb_build_object('merged_from_node_id', loser.node_id)
    FROM rvbbit.kg_aliases
    WHERE node_id = loser.node_id
    ON CONFLICT (graph_id, kind, alias_norm) DO UPDATE SET
        node_id = EXCLUDED.node_id,
        alias = EXCLUDED.alias,
        confidence = greatest(rvbbit.kg_aliases.confidence, EXCLUDED.confidence),
        properties = rvbbit.kg_aliases.properties || EXCLUDED.properties;

    PERFORM rvbbit.kg_assert_alias(
        winner.node_id,
        loser.label,
        loser.confidence,
        jsonb_build_object('merged_from_node_id', loser.node_id),
        winner.graph_id
    );

    UPDATE rvbbit.kg_evidence
    SET node_id = winner.node_id
    WHERE node_id = loser.node_id;

    FOR edge_row IN
        SELECT *
        FROM rvbbit.kg_edges
        WHERE subject_node_id = loser.node_id
           OR object_node_id = loser.node_id
        ORDER BY edge_id
    LOOP
        new_subject_id := CASE WHEN edge_row.subject_node_id = loser.node_id THEN winner.node_id ELSE edge_row.subject_node_id END;
        new_object_id := CASE WHEN edge_row.object_node_id = loser.node_id THEN winner.node_id ELSE edge_row.object_node_id END;

        IF new_subject_id = new_object_id THEN
            UPDATE rvbbit.kg_evidence
            SET node_id = winner.node_id,
                edge_id = NULL
            WHERE edge_id = edge_row.edge_id;
            DELETE FROM rvbbit.kg_edges WHERE edge_id = edge_row.edge_id;
            CONTINUE;
        END IF;

        SELECT e.edge_id INTO existing_edge_id
        FROM rvbbit.kg_edges e
        WHERE e.subject_node_id = new_subject_id
          AND e.graph_id = winner.graph_id
          AND e.predicate_norm = edge_row.predicate_norm
          AND e.object_node_id = new_object_id
          AND e.edge_id <> edge_row.edge_id
        LIMIT 1;

        IF existing_edge_id IS NOT NULL THEN
            UPDATE rvbbit.kg_edges e
            SET properties = e.properties || edge_row.properties,
                confidence = greatest(e.confidence, edge_row.confidence)
            WHERE e.edge_id = existing_edge_id;

            UPDATE rvbbit.kg_evidence
            SET edge_id = existing_edge_id
            WHERE edge_id = edge_row.edge_id;

            DELETE FROM rvbbit.kg_edges WHERE edge_id = edge_row.edge_id;
        ELSE
            UPDATE rvbbit.kg_edges
            SET subject_node_id = new_subject_id,
                object_node_id = new_object_id
            WHERE edge_id = edge_row.edge_id;
        END IF;

        existing_edge_id := NULL;
    END LOOP;

    UPDATE rvbbit.kg_merge_candidates
    SET status = 'superseded',
        reviewed_at = now(),
        properties = properties || jsonb_build_object('superseded_by_merge_id', out_merge_id)
    WHERE status = 'pending'
      AND candidate_id IS DISTINCT FROM merge_candidate_id
      AND graph_id = winner.graph_id
      AND (left_node_id = loser.node_id OR right_node_id = loser.node_id);

    DELETE FROM rvbbit.kg_nodes
    WHERE node_id = loser.node_id;

    RETURN out_merge_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_accept_merge(
    target_candidate_id bigint,
    preferred_winner_node_id bigint DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    candidate rvbbit.kg_merge_candidates%ROWTYPE;
    left_conf double precision;
    right_conf double precision;
    chosen_winner_id bigint;
    chosen_loser_id bigint;
    out_merge_id bigint;
BEGIN
    SELECT * INTO candidate
    FROM rvbbit.kg_merge_candidates
    WHERE candidate_id = target_candidate_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.kg_accept_merge: candidate % not found', target_candidate_id;
    END IF;

    IF candidate.status = 'accepted' THEN
        SELECT merge_id INTO out_merge_id
        FROM rvbbit.kg_node_merges
        WHERE candidate_id = target_candidate_id
        ORDER BY merge_id DESC
        LIMIT 1;
        RETURN out_merge_id;
    END IF;

    IF candidate.status <> 'pending' THEN
        RAISE EXCEPTION 'rvbbit.kg_accept_merge: candidate % is %, not pending', target_candidate_id, candidate.status;
    END IF;

    IF preferred_winner_node_id IS NOT NULL THEN
        IF preferred_winner_node_id NOT IN (candidate.left_node_id, candidate.right_node_id) THEN
            RAISE EXCEPTION 'rvbbit.kg_accept_merge: preferred winner % is not part of candidate %',
                preferred_winner_node_id, target_candidate_id;
        END IF;
        chosen_winner_id := preferred_winner_node_id;
    ELSE
        SELECT confidence INTO left_conf FROM rvbbit.kg_nodes WHERE node_id = candidate.left_node_id;
        SELECT confidence INTO right_conf FROM rvbbit.kg_nodes WHERE node_id = candidate.right_node_id;
        IF COALESCE(left_conf, 0.0) >= COALESCE(right_conf, 0.0) THEN
            chosen_winner_id := candidate.left_node_id;
        ELSE
            chosen_winner_id := candidate.right_node_id;
        END IF;
    END IF;

    chosen_loser_id := CASE
        WHEN chosen_winner_id = candidate.left_node_id THEN candidate.right_node_id
        ELSE candidate.left_node_id
    END;

    out_merge_id := rvbbit.kg_merge_nodes(
        chosen_winner_id,
        chosen_loser_id,
        target_candidate_id,
        jsonb_build_object('accepted_candidate_id', target_candidate_id)
    );

    UPDATE rvbbit.kg_merge_candidates
    SET status = 'accepted',
        reviewed_at = now(),
        query_id = COALESCE(query_id, rvbbit.current_query_id()),
        properties = properties || jsonb_build_object(
            'merge_id', out_merge_id,
            'winner_node_id', chosen_winner_id,
            'loser_node_id', chosen_loser_id
        )
    WHERE candidate_id = target_candidate_id;

    RETURN out_merge_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_neighbors(
    node_kind text,
    node_label text,
    max_depth int DEFAULT 1,
    direction text DEFAULT 'both',
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS TABLE (
    depth int,
    edge_id bigint,
    from_node_id bigint,
    from_kind text,
    from_label text,
    predicate text,
    to_node_id bigint,
    to_kind text,
    to_label text,
    confidence double precision,
    properties jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    start_id bigint;
    max_d int;
    dir text;
    norm_graph text;
BEGIN
    max_d := greatest(COALESCE(max_depth, 1), 0);
    dir := lower(COALESCE(direction, 'both'));
    norm_graph := rvbbit.kg_normalize_graph(graph);
    IF dir NOT IN ('out', 'in', 'both') THEN
        RAISE EXCEPTION 'rvbbit.kg_neighbors: direction must be out, in, or both';
    END IF;
    IF max_d = 0 THEN
        RETURN;
    END IF;

    SELECT r.node_id INTO start_id
    FROM rvbbit.kg_resolve_node(node_kind, node_label, specialist, match_threshold, norm_graph) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;
    IF start_id IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH RECURSIVE adj AS (
        SELECT e.edge_id, e.subject_node_id AS src, e.object_node_id AS dst,
               e.predicate, e.confidence, e.properties
        FROM rvbbit.kg_edges e
        WHERE dir IN ('out', 'both')
          AND e.graph_id = norm_graph
        UNION ALL
        SELECT e.edge_id, e.object_node_id AS src, e.subject_node_id AS dst,
               e.predicate, e.confidence, e.properties
        FROM rvbbit.kg_edges e
        WHERE dir IN ('in', 'both')
          AND e.graph_id = norm_graph
    ),
    walk(depth, edge_id, from_node_id, to_node_id, path_nodes) AS (
        SELECT 1, a.edge_id, a.src, a.dst, ARRAY[start_id, a.dst]::bigint[]
        FROM adj a
        WHERE a.src = start_id
        UNION ALL
        SELECT w.depth + 1, a.edge_id, a.src, a.dst, w.path_nodes || a.dst
        FROM walk w
        JOIN adj a ON a.src = w.to_node_id
        WHERE w.depth < max_d
          AND NOT a.dst = ANY(w.path_nodes)
    )
    SELECT
        w.depth,
        e.edge_id,
        from_n.node_id,
        from_n.kind,
        from_n.label,
        e.predicate,
        to_n.node_id,
        to_n.kind,
        to_n.label,
        e.confidence,
        e.properties
    FROM walk w
    JOIN rvbbit.kg_edges e ON e.edge_id = w.edge_id
    JOIN rvbbit.kg_nodes from_n ON from_n.node_id = w.from_node_id
    JOIN rvbbit.kg_nodes to_n ON to_n.node_id = w.to_node_id
    ORDER BY w.depth, e.confidence DESC, e.edge_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_paths(
    subject_kind text,
    subject_label text,
    object_kind text,
    object_label text,
    max_depth int DEFAULT 3,
    direction text DEFAULT 'out',
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS TABLE (
    length int,
    edge_ids bigint[],
    node_ids bigint[],
    labels text[]
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    start_id bigint;
    target_id bigint;
    start_label text;
    max_d int;
    dir text;
    norm_graph text;
BEGIN
    max_d := greatest(COALESCE(max_depth, 3), 0);
    dir := lower(COALESCE(direction, 'out'));
    norm_graph := rvbbit.kg_normalize_graph(graph);
    IF dir NOT IN ('out', 'in', 'both') THEN
        RAISE EXCEPTION 'rvbbit.kg_paths: direction must be out, in, or both';
    END IF;
    IF max_d = 0 THEN
        RETURN;
    END IF;

    SELECT r.node_id, r.label INTO start_id, start_label
    FROM rvbbit.kg_resolve_node(subject_kind, subject_label, specialist, match_threshold, norm_graph) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;
    SELECT r.node_id INTO target_id
    FROM rvbbit.kg_resolve_node(object_kind, object_label, specialist, match_threshold, norm_graph) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;
    IF start_id IS NULL OR target_id IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH RECURSIVE adj AS (
        SELECT e.edge_id, e.subject_node_id AS src, e.object_node_id AS dst
        FROM rvbbit.kg_edges e
        WHERE dir IN ('out', 'both')
          AND e.graph_id = norm_graph
        UNION ALL
        SELECT e.edge_id, e.object_node_id AS src, e.subject_node_id AS dst
        FROM rvbbit.kg_edges e
        WHERE dir IN ('in', 'both')
          AND e.graph_id = norm_graph
    ),
    walk(node_id, edge_ids, node_ids, labels, depth) AS (
        SELECT start_id, ARRAY[]::bigint[], ARRAY[start_id]::bigint[], ARRAY[start_label]::text[], 0
        UNION ALL
        SELECT a.dst,
               w.edge_ids || a.edge_id,
               w.node_ids || a.dst,
               w.labels || n.label,
               w.depth + 1
        FROM walk w
        JOIN adj a ON a.src = w.node_id
        JOIN rvbbit.kg_nodes n ON n.node_id = a.dst
        WHERE w.depth < max_d
          AND NOT a.dst = ANY(w.node_ids)
    )
    SELECT w.depth, w.edge_ids, w.node_ids, w.labels
    FROM walk w
    WHERE w.node_id = target_id
      AND w.depth > 0
    ORDER BY w.depth, w.edge_ids
    LIMIT 100;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_context(
    node_kind text,
    node_label text,
    max_depth int DEFAULT 2,
    max_edges int DEFAULT 100,
    direction text DEFAULT 'both',
    include_evidence boolean DEFAULT true,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL,
    ranking jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE (
    context_rank int,
    score double precision,
    depth int,
    edge_id bigint,
    from_node_id bigint,
    from_kind text,
    from_label text,
    predicate text,
    to_node_id bigint,
    to_kind text,
    to_label text,
    edge_direction text,
    edge_confidence double precision,
    edge_properties jsonb,
    path_node_ids bigint[],
    path_edge_ids bigint[],
    evidence_count bigint,
    evidence jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    start_id bigint;
    max_d int;
    max_rows int;
    dir text;
    with_evidence boolean := COALESCE(include_evidence, true);
    norm_graph text;
    ranking_doc jsonb := COALESCE(ranking, '{}'::jsonb);
    depth_decay double precision := 0.85;
BEGIN
    max_d := greatest(COALESCE(max_depth, 2), 0);
    max_rows := greatest(COALESCE(max_edges, 100), 1);
    dir := lower(COALESCE(direction, 'both'));
    norm_graph := rvbbit.kg_normalize_graph(graph);
    IF ranking_doc ? 'depth_decay' THEN
        BEGIN
            depth_decay := (ranking_doc->>'depth_decay')::double precision;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'rvbbit.kg_context: ranking.depth_decay must be a number';
        END;
    END IF;
    IF depth_decay < 0.0 OR depth_decay > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_context: ranking.depth_decay must be between 0 and 1';
    END IF;
    IF dir NOT IN ('out', 'in', 'both') THEN
        RAISE EXCEPTION 'rvbbit.kg_context: direction must be out, in, or both';
    END IF;
    IF max_d = 0 THEN
        RETURN;
    END IF;

    SELECT r.node_id INTO start_id
    FROM rvbbit.kg_resolve_node(node_kind, node_label, specialist, match_threshold, norm_graph) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;
    IF start_id IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH RECURSIVE adj AS (
        SELECT e.edge_id AS a_edge_id,
               e.subject_node_id AS a_from_node_id,
               e.object_node_id AS a_to_node_id,
               'out'::text AS a_edge_direction,
               e.predicate AS a_predicate,
               e.confidence AS a_edge_confidence,
               e.properties AS a_edge_properties
        FROM rvbbit.kg_edges e
        WHERE dir IN ('out', 'both')
          AND e.graph_id = norm_graph
        UNION ALL
        SELECT e.edge_id AS a_edge_id,
               e.object_node_id AS a_from_node_id,
               e.subject_node_id AS a_to_node_id,
               'in'::text AS a_edge_direction,
               e.predicate AS a_predicate,
               e.confidence AS a_edge_confidence,
               e.properties AS a_edge_properties
        FROM rvbbit.kg_edges e
        WHERE dir IN ('in', 'both')
          AND e.graph_id = norm_graph
    ),
    walk AS (
        SELECT 1 AS w_depth,
               a.a_edge_id AS w_edge_id,
               a.a_from_node_id AS w_from_node_id,
               a.a_to_node_id AS w_to_node_id,
               a.a_edge_direction AS w_edge_direction,
               ARRAY[start_id, a.a_to_node_id]::bigint[] AS w_path_node_ids,
               ARRAY[a.a_edge_id]::bigint[] AS w_path_edge_ids,
               a.a_edge_confidence::double precision AS w_score
        FROM adj a
        WHERE a.a_from_node_id = start_id
        UNION ALL
        SELECT w.w_depth + 1 AS w_depth,
               a.a_edge_id AS w_edge_id,
               a.a_from_node_id AS w_from_node_id,
               a.a_to_node_id AS w_to_node_id,
               a.a_edge_direction AS w_edge_direction,
               w.w_path_node_ids || a.a_to_node_id,
               w.w_path_edge_ids || a.a_edge_id,
               (w.w_score * a.a_edge_confidence * depth_decay)::double precision AS w_score
        FROM walk w
        JOIN adj a ON a.a_from_node_id = w.w_to_node_id
        WHERE w.w_depth < max_d
          AND NOT a.a_to_node_id = ANY(w.w_path_node_ids)
    ),
    best AS (
        SELECT DISTINCT ON (w.w_edge_id)
               w.w_depth,
               w.w_edge_id,
               w.w_from_node_id,
               w.w_to_node_id,
               w.w_edge_direction,
               w.w_path_node_ids,
               w.w_path_edge_ids,
               w.w_score
        FROM walk w
        ORDER BY w.w_edge_id, w.w_score DESC, w.w_depth, w.w_path_edge_ids
    ),
    ranked AS (
        SELECT row_number() OVER (ORDER BY b.w_score DESC, b.w_depth, b.w_edge_id)::int AS r_context_rank,
               b.w_score AS r_score,
               b.w_depth AS r_depth,
               b.w_edge_id AS r_edge_id,
               b.w_from_node_id AS r_from_node_id,
               b.w_to_node_id AS r_to_node_id,
               b.w_edge_direction AS r_edge_direction,
               b.w_path_node_ids AS r_path_node_ids,
               b.w_path_edge_ids AS r_path_edge_ids
        FROM best b
        ORDER BY b.w_score DESC, b.w_depth, b.w_edge_id
        LIMIT max_rows
    )
    SELECT ranked.r_context_rank,
           ranked.r_score,
           ranked.r_depth,
           ranked.r_edge_id,
           from_n.node_id,
           from_n.kind,
           from_n.label,
           e.predicate,
           to_n.node_id,
           to_n.kind,
           to_n.label,
           ranked.r_edge_direction,
           e.confidence,
           e.properties,
           ranked.r_path_node_ids,
           ranked.r_path_edge_ids,
           ev.evidence_count,
           ev.evidence
    FROM ranked
    JOIN rvbbit.kg_edges e ON e.edge_id = ranked.r_edge_id
    JOIN rvbbit.kg_nodes from_n ON from_n.node_id = ranked.r_from_node_id
    JOIN rvbbit.kg_nodes to_n ON to_n.node_id = ranked.r_to_node_id
    CROSS JOIN LATERAL (
        SELECT CASE WHEN with_evidence THEN count(ev_row.evidence_id) ELSE 0 END::bigint AS evidence_count,
               CASE
                   WHEN with_evidence THEN COALESCE(
                       jsonb_agg(
                           jsonb_build_object(
                               'evidence_id', ev_row.evidence_id,
                               'target', CASE
                                   WHEN ev_row.edge_id = ranked.r_edge_id THEN 'edge'
                                   WHEN ev_row.node_id = ranked.r_to_node_id THEN 'to_node'
                                   ELSE 'from_node'
                               END,
                               'query_id', ev_row.query_id,
                               'source_table', ev_row.source_table::text,
                               'source_pk', ev_row.source_pk,
                               'source_column', ev_row.source_column,
                               'evidence_text', ev_row.evidence_text,
                               'confidence', ev_row.confidence,
                               'properties', ev_row.properties
                           )
                           ORDER BY
                               CASE WHEN ev_row.edge_id = ranked.r_edge_id THEN 0 ELSE 1 END,
                               ev_row.confidence DESC,
                               ev_row.evidence_id
                       ) FILTER (WHERE ev_row.evidence_id IS NOT NULL),
                       '[]'::jsonb
                   )
                   ELSE '[]'::jsonb
               END AS evidence
        FROM rvbbit.kg_evidence ev_row
        WHERE with_evidence
          AND ev_row.graph_id = norm_graph
          AND (
              ev_row.edge_id = ranked.r_edge_id
              OR ev_row.node_id = ranked.r_to_node_id
          )
    ) ev
    ORDER BY ranked.r_context_rank;
END $$;
/* </end connected objects> */

/* <begin connected objects> */
-- crates/pg_rvbbit/src/triples.rs:10
-- requires:
--   kg_bootstrap


-- First-class triple extraction ---------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.triples_valid(output text, inputs jsonb DEFAULT '{}'::jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    doc jsonb;
    item_doc jsonb;
    conf double precision;
BEGIN
    IF output IS NULL OR btrim(output) = '' THEN
        RETURN false;
    END IF;

    BEGIN
        doc := output::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN false;
    END;

    IF jsonb_typeof(doc) <> 'array' THEN
        RETURN false;
    END IF;

    FOR item_doc IN SELECT elem.value FROM jsonb_array_elements(doc) AS elem(value) LOOP
        IF jsonb_typeof(item_doc) <> 'object' THEN
            RETURN false;
        END IF;

        IF btrim(COALESCE(item_doc->>'subject', '')) = ''
           OR btrim(COALESCE(item_doc->>'predicate', '')) = ''
           OR btrim(COALESCE(item_doc->>'object', '')) = '' THEN
            RETURN false;
        END IF;

        IF (item_doc - ARRAY[
            'subject_kind', 'subject', 'predicate', 'object_kind', 'object',
            'confidence', 'evidence', 'properties'
        ]) <> '{}'::jsonb THEN
            RETURN false;
        END IF;

        IF item_doc ? 'confidence' THEN
            BEGIN
                conf := (item_doc->>'confidence')::double precision;
            EXCEPTION WHEN OTHERS THEN
                RETURN false;
            END;
            IF conf < 0.0 OR conf > 1.0 THEN
                RETURN false;
            END IF;
        END IF;

        IF item_doc ? 'properties' AND jsonb_typeof(item_doc->'properties') <> 'object' THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN true;
END $$;

DO $$
BEGIN
    PERFORM rvbbit.create_operator(
        op_name => 'triples',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text', 'focus'],
        op_arg_types => ARRAY['text', 'text'],
        op_return_type => 'jsonb',
        op_model => 'openai/gpt-5.4-mini',
        op_parser => 'json',
        op_max_tokens => 1200,
        op_temperature => 0.0,
        op_description =>
            'Extract knowledge graph triples from text as strict JSON. ' ||
            'Editable seed operator used by rvbbit.triples_rows and KG ingestion.',
        op_system =>
            'You are a strict knowledge graph extraction engine. Extract concise, useful facts as JSON triples. ' ||
            'Return ONLY a valid JSON array. Each item MUST use exactly these keys unless optional values are needed: ' ||
            'subject_kind, subject, predicate, object_kind, object, confidence, evidence, properties. ' ||
            'subject and object are entity/value labels. subject_kind and object_kind are short lowercase types such as ' ||
            'person, organization, customer, product, issue, event, metric, document, place, date, value, or concept. ' ||
            'predicate is a snake_case relationship such as works_at, reported, affects, requested, approved, located_in, ' ||
            'uses, owns, depends_on, caused_by, deadline_is, has_status. confidence is 0.0 to 1.0. ' ||
            'evidence is a short quote or sentence from the input. properties is an optional object. ' ||
            'Extract explicit facts first. Include only high-signal facts. Empty input or no facts returns []. ' ||
            'No markdown, no commentary, no code fence.',
        op_user =>
            E'FOCUS: {{ focus }}\n\nTEXT:\n{{ text }}\n\nReturn JSON array only.',
        op_tests => NULL
    );

    PERFORM rvbbit.set_operator_retry(
        'triples',
        $cfg${
          "until": {"function": "rvbbit.triples_valid"},
          "max_attempts": 4,
          "instructions": "Your previous output was invalid. Return ONLY a JSON array. Each item must include non-empty subject, predicate, and object; predicate must be snake_case; confidence must be between 0 and 1; no markdown or extra keys."
        }$cfg$::jsonb
    );
END $$;

CREATE OR REPLACE FUNCTION rvbbit.triples_json_rows(raw jsonb)
RETURNS TABLE (
    subject_kind text,
    subject text,
    predicate text,
    object_kind text,
    object text,
    confidence double precision,
    evidence text,
    properties jsonb
)
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    item jsonb;
    conf double precision;
    props jsonb;
BEGIN
    IF raw IS NULL OR jsonb_typeof(raw) <> 'array' THEN
        RETURN;
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(raw) LOOP
        IF jsonb_typeof(item) <> 'object' THEN
            CONTINUE;
        END IF;
        subject := NULLIF(btrim(COALESCE(item->>'subject', '')), '');
        predicate := NULLIF(btrim(COALESCE(item->>'predicate', '')), '');
        object := NULLIF(btrim(COALESCE(item->>'object', '')), '');
        IF subject IS NULL OR predicate IS NULL OR object IS NULL THEN
            CONTINUE;
        END IF;

        subject_kind := COALESCE(NULLIF(btrim(item->>'subject_kind'), ''), 'entity');
        object_kind := COALESCE(NULLIF(btrim(item->>'object_kind'), ''), 'entity');

        BEGIN
            conf := COALESCE(NULLIF(item->>'confidence', '')::double precision, 1.0);
        EXCEPTION WHEN OTHERS THEN
            conf := 1.0;
        END;
        confidence := least(greatest(conf, 0.0), 1.0);

        evidence := NULLIF(COALESCE(item->>'evidence', item->>'quote', item->>'text'), '');
        props := CASE
            WHEN jsonb_typeof(item->'properties') = 'object' THEN item->'properties'
            ELSE '{}'::jsonb
        END;
        properties := props || (item - ARRAY[
            'subject_kind', 'subject', 'predicate', 'object_kind', 'object',
            'confidence', 'evidence', 'quote', 'text', 'properties'
        ]);

        RETURN NEXT;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.triples_rows(
    input_text text,
    focus text DEFAULT 'all',
    opts jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE (
    subject_kind text,
    subject text,
    predicate text,
    object_kind text,
    object text,
    confidence double precision,
    evidence text,
    properties jsonb
)
LANGUAGE sql
VOLATILE
AS $$
    SELECT *
    FROM rvbbit.triples_json_rows(
        rvbbit.triples(input_text, COALESCE(focus, 'all'), COALESCE(opts, '{}'::jsonb))
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.kg_ingest_triples(
    triples_sql text,
    source_table regclass DEFAULT NULL,
    source_pk text DEFAULT NULL,
    source_column text DEFAULT NULL,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    row_doc jsonb;
    subj_kind text;
    subj text;
    pred text;
    obj_kind text;
    obj text;
    conf double precision;
    ev_text text;
    props jsonb;
    row_source_table regclass;
    row_source_pk text;
    row_source_column text;
    row_graph text;
    norm_graph text := rvbbit.kg_normalize_graph(graph);
    edge_id bigint;
    inserted bigint := 0;
BEGIN
    IF triples_sql IS NULL OR btrim(triples_sql) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_ingest_triples: triples_sql must be non-empty';
    END IF;

    FOR row_doc IN EXECUTE format('SELECT to_jsonb(q) FROM (%s) AS q', triples_sql) LOOP
        subj := NULLIF(btrim(COALESCE(row_doc->>'subject', '')), '');
        pred := NULLIF(btrim(COALESCE(row_doc->>'predicate', '')), '');
        obj := NULLIF(btrim(COALESCE(row_doc->>'object', '')), '');
        IF subj IS NULL OR pred IS NULL OR obj IS NULL THEN
            CONTINUE;
        END IF;

        subj_kind := COALESCE(NULLIF(btrim(row_doc->>'subject_kind'), ''), 'entity');
        obj_kind := COALESCE(NULLIF(btrim(row_doc->>'object_kind'), ''), 'entity');
        BEGIN
            conf := COALESCE(NULLIF(row_doc->>'confidence', '')::double precision, 1.0);
        EXCEPTION WHEN OTHERS THEN
            conf := 1.0;
        END;
        conf := least(greatest(conf, 0.0), 1.0);

        ev_text := NULLIF(COALESCE(row_doc->>'evidence', row_doc->>'quote', row_doc->>'text'), '');
        props := CASE
            WHEN jsonb_typeof(row_doc->'properties') = 'object' THEN row_doc->'properties'
            ELSE '{}'::jsonb
        END;

        row_source_pk := COALESCE(NULLIF(row_doc->>'source_pk', ''), source_pk);
        row_source_column := COALESCE(NULLIF(row_doc->>'source_column', ''), source_column);
        row_graph := COALESCE(NULLIF(row_doc->>'graph_id', ''), norm_graph);
        row_graph := rvbbit.kg_normalize_graph(row_graph);
        IF row_doc ? 'source_table' AND NULLIF(row_doc->>'source_table', '') IS NOT NULL THEN
            BEGIN
                row_source_table := (row_doc->>'source_table')::regclass;
            EXCEPTION WHEN OTHERS THEN
                row_source_table := source_table;
            END;
        ELSE
            row_source_table := source_table;
        END IF;

        edge_id := rvbbit.kg_assert_edge(
            subj_kind, subj, pred, obj_kind, obj, conf,
            '{}'::jsonb, props, specialist, match_threshold, row_graph
        );

        IF ev_text IS NOT NULL
           OR row_source_table IS NOT NULL
           OR row_source_pk IS NOT NULL
           OR row_source_column IS NOT NULL THEN
            PERFORM rvbbit.kg_link_evidence(
                target_edge_id => edge_id,
                source_table => row_source_table,
                source_pk => row_source_pk,
                source_column => row_source_column,
                evidence_text => ev_text,
                confidence => conf,
                properties => row_doc,
                graph => row_graph
            );
        END IF;

        inserted := inserted + 1;
    END LOOP;

    RETURN inserted;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.kg_ingest_table(
    source_rel regclass,
    pk_col text,
    text_col text,
    focus text DEFAULT 'all',
    graph text DEFAULT NULL,
    limit_rows int DEFAULT NULL,
    where_sql text DEFAULT NULL,
    opts jsonb DEFAULT '{}'::jsonb,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92
) RETURNS TABLE (
    run_id bigint,
    rows_seen bigint,
    triples_inserted bigint,
    errors bigint
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    norm_graph text := rvbbit.kg_normalize_graph(graph);
    qid uuid := rvbbit.current_query_id();
    run bigint;
    row_rec record;
    select_sql text;
    triples_sql text;
    n bigint;
    seen bigint := 0;
    inserted bigint := 0;
    err_count bigint := 0;
BEGIN
    IF source_rel IS NULL THEN
        RAISE EXCEPTION 'rvbbit.kg_ingest_table: source_rel is required';
    END IF;
    IF pk_col IS NULL OR btrim(pk_col) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_ingest_table: pk_col is required';
    END IF;
    IF text_col IS NULL OR btrim(text_col) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_ingest_table: text_col is required';
    END IF;
    IF limit_rows IS NOT NULL AND limit_rows <= 0 THEN
        RAISE EXCEPTION 'rvbbit.kg_ingest_table: limit_rows must be positive';
    END IF;

    INSERT INTO rvbbit.kg_extraction_runs(
        graph_id, query_id, source_table, source_column, focus, status, properties
    )
    VALUES (
        norm_graph, qid, source_rel, text_col, COALESCE(focus, 'all'), 'running',
        jsonb_build_object('pk_col', pk_col, 'where_sql', where_sql, 'limit_rows', limit_rows)
    )
    RETURNING kg_extraction_runs.run_id INTO run;

    select_sql := format(
        'SELECT %1$I::text AS source_pk, %2$I::text AS input_text FROM %3$s WHERE %2$I IS NOT NULL AND btrim(%2$I::text) <> ''''',
        pk_col, text_col, source_rel
    );
    IF where_sql IS NOT NULL AND btrim(where_sql) <> '' THEN
        select_sql := select_sql || ' AND (' || where_sql || ')';
    END IF;
    select_sql := select_sql || format(' ORDER BY %I', pk_col);
    IF limit_rows IS NOT NULL THEN
        select_sql := select_sql || format(' LIMIT %s', limit_rows);
    END IF;

    FOR row_rec IN EXECUTE select_sql LOOP
        seen := seen + 1;
        BEGIN
            triples_sql := format(
                'SELECT *, %L::text AS source_pk, %L::text AS source_column, %L::text AS source_table, %L::text AS graph_id FROM rvbbit.triples_rows(%L, %L, %L::jsonb)',
                row_rec.source_pk,
                text_col,
                source_rel::text,
                norm_graph,
                row_rec.input_text,
                COALESCE(focus, 'all'),
                COALESCE(opts, '{}'::jsonb)::text
            );
            n := rvbbit.kg_ingest_triples(
                triples_sql,
                source_rel,
                row_rec.source_pk,
                text_col,
                specialist,
                match_threshold,
                norm_graph
            );
            inserted := inserted + COALESCE(n, 0);
        EXCEPTION WHEN OTHERS THEN
            err_count := err_count + 1;
            INSERT INTO rvbbit.kg_extraction_errors(
                run_id, graph_id, query_id, source_table, source_pk, source_column,
                input_text, error, properties
            )
            VALUES (
                run, norm_graph, qid, source_rel, row_rec.source_pk, text_col,
                row_rec.input_text, SQLERRM, jsonb_build_object('sqlstate', SQLSTATE)
            );
        END;
    END LOOP;

    UPDATE rvbbit.kg_extraction_runs
    SET rows_seen = seen,
        triples_inserted = inserted,
        errors = err_count,
        status = CASE
            WHEN err_count = 0 THEN 'ok'
            WHEN inserted > 0 THEN 'partial'
            ELSE 'failed'
        END,
        finished_at = now()
    WHERE kg_extraction_runs.run_id = run;

    run_id := run;
    rows_seen := seen;
    triples_inserted := inserted;
    errors := err_count;
    RETURN NEXT;
END $$;
