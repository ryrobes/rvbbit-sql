-- SQL-native knowledge graph primitives.

CREATE TABLE IF NOT EXISTS rvbbit.kg_nodes (
    node_id     bigserial PRIMARY KEY,
    kind        text NOT NULL,
    label       text NOT NULL,
    label_norm  text NOT NULL,
    properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence  double precision NOT NULL DEFAULT 1.0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_nodes_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT kg_nodes_kind_label_unique UNIQUE (kind, label_norm)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_aliases (
    alias_id    bigserial PRIMARY KEY,
    node_id     bigint NOT NULL REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    kind        text NOT NULL,
    alias       text NOT NULL,
    alias_norm  text NOT NULL,
    confidence  double precision NOT NULL DEFAULT 1.0,
    properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_aliases_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT kg_aliases_kind_alias_unique UNIQUE (kind, alias_norm)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_edges (
    edge_id          bigserial PRIMARY KEY,
    subject_node_id  bigint NOT NULL REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    predicate        text NOT NULL,
    predicate_norm   text NOT NULL,
    object_node_id   bigint NOT NULL REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    properties       jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence       double precision NOT NULL DEFAULT 1.0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kg_edges_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT kg_edges_unique_fact UNIQUE (subject_node_id, predicate_norm, object_node_id)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_evidence (
    evidence_id   bigserial PRIMARY KEY,
    edge_id       bigint REFERENCES rvbbit.kg_edges(edge_id) ON DELETE CASCADE,
    node_id       bigint REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS kg_aliases_node_idx ON rvbbit.kg_aliases(node_id);
CREATE INDEX IF NOT EXISTS kg_edges_subject_idx ON rvbbit.kg_edges(subject_node_id);
CREATE INDEX IF NOT EXISTS kg_edges_object_idx ON rvbbit.kg_edges(object_node_id);
CREATE INDEX IF NOT EXISTS kg_edges_predicate_idx ON rvbbit.kg_edges(predicate_norm);
CREATE INDEX IF NOT EXISTS kg_evidence_edge_idx ON rvbbit.kg_evidence(edge_id);
CREATE INDEX IF NOT EXISTS kg_evidence_node_idx ON rvbbit.kg_evidence(node_id);
CREATE INDEX IF NOT EXISTS kg_evidence_source_idx
    ON rvbbit.kg_evidence(source_table, source_pk)
    WHERE source_table IS NOT NULL OR source_pk IS NOT NULL;

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

CREATE OR REPLACE FUNCTION rvbbit.kg_assert_alias(
    target_node_id bigint,
    alias_label text,
    confidence double precision DEFAULT 1.0,
    properties jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    node_kind text;
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

    SELECT kind INTO node_kind
    FROM rvbbit.kg_nodes
    WHERE node_id = target_node_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_alias: node_id % not found', target_node_id;
    END IF;

    norm_alias := rvbbit.kg_normalize_label(alias_label);
    alias_props := COALESCE(properties, '{}'::jsonb);
    INSERT INTO rvbbit.kg_aliases(node_id, kind, alias, alias_norm, confidence, properties)
    VALUES (target_node_id, node_kind, btrim(alias_label), norm_alias, confidence, alias_props)
    ON CONFLICT (kind, alias_norm) DO UPDATE SET
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
    match_threshold double precision DEFAULT 0.92
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

    RETURN QUERY
    SELECT n.node_id, n.kind, n.label, 1.0::double precision, 'alias'::text
    FROM rvbbit.kg_aliases a
    JOIN rvbbit.kg_nodes n ON n.node_id = a.node_id
    WHERE a.kind = norm_kind
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
        WHERE n.kind = norm_kind
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
    match_threshold double precision DEFAULT 0.92
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    norm_kind text;
    norm_label text;
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
    node_props := COALESCE(properties, '{}'::jsonb);

    SELECT r.node_id INTO resolved_id
    FROM rvbbit.kg_resolve_node(norm_kind, node_label, specialist, match_threshold) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;

    IF resolved_id IS NOT NULL THEN
        UPDATE rvbbit.kg_nodes
        SET properties = rvbbit.kg_nodes.properties || node_props,
            confidence = greatest(rvbbit.kg_nodes.confidence, kg_assert_node.confidence)
        WHERE node_id = resolved_id;

        PERFORM rvbbit.kg_assert_alias(resolved_id, node_label, confidence);
        RETURN resolved_id;
    END IF;

    INSERT INTO rvbbit.kg_nodes(kind, label, label_norm, properties, confidence)
    VALUES (norm_kind, btrim(node_label), norm_label, node_props, confidence)
    ON CONFLICT (kind, label_norm) DO UPDATE SET
        label = EXCLUDED.label,
        properties = rvbbit.kg_nodes.properties || EXCLUDED.properties,
        confidence = greatest(rvbbit.kg_nodes.confidence, EXCLUDED.confidence)
    RETURNING node_id INTO out_node_id;

    PERFORM rvbbit.kg_assert_alias(out_node_id, node_label, confidence);
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
    span int4range DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    out_evidence_id bigint;
BEGIN
    IF target_edge_id IS NULL AND target_node_id IS NULL THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: target_edge_id or target_node_id is required';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: confidence must be between 0 and 1';
    END IF;

    INSERT INTO rvbbit.kg_evidence(
        edge_id, node_id, source_table, source_pk, source_column,
        evidence_text, span, confidence, properties
    )
    VALUES (
        target_edge_id, target_node_id, source_table, source_pk, source_column,
        evidence_text, span, confidence, COALESCE(properties, '{}'::jsonb)
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
    match_threshold double precision DEFAULT 0.92
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
BEGIN
    IF predicate IS NULL OR btrim(predicate) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_edge: predicate must be non-empty';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_assert_edge: confidence must be between 0 and 1';
    END IF;

    subj_id := rvbbit.kg_assert_node(subject_kind, subject_label, '{}'::jsonb, confidence, specialist, match_threshold);
    obj_id := rvbbit.kg_assert_node(object_kind, object_label, '{}'::jsonb, confidence, specialist, match_threshold);
    norm_pred := rvbbit.kg_normalize_predicate(predicate);
    edge_props := COALESCE(properties, '{}'::jsonb);
    evidence_doc := COALESCE(evidence, '{}'::jsonb);

    INSERT INTO rvbbit.kg_edges(subject_node_id, predicate, predicate_norm, object_node_id, properties, confidence)
    VALUES (subj_id, btrim(predicate), norm_pred, obj_id, edge_props, confidence)
    ON CONFLICT (subject_node_id, predicate_norm, object_node_id) DO UPDATE SET
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
            properties => evidence_doc
        );
    END IF;

    RETURN out_edge_id;
END $$;

DROP FUNCTION IF EXISTS rvbbit.kg_neighbors(text, text, int, text, text, double precision);

CREATE OR REPLACE FUNCTION rvbbit.kg_neighbors(
    node_kind text,
    node_label text,
    max_depth int DEFAULT 1,
    direction text DEFAULT 'both',
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92
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
BEGIN
    max_d := greatest(COALESCE(max_depth, 1), 0);
    dir := lower(COALESCE(direction, 'both'));
    IF dir NOT IN ('out', 'in', 'both') THEN
        RAISE EXCEPTION 'rvbbit.kg_neighbors: direction must be out, in, or both';
    END IF;
    IF max_d = 0 THEN
        RETURN;
    END IF;

    SELECT r.node_id INTO start_id
    FROM rvbbit.kg_resolve_node(node_kind, node_label, specialist, match_threshold) r
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
        UNION ALL
        SELECT e.edge_id, e.object_node_id AS src, e.subject_node_id AS dst,
               e.predicate, e.confidence, e.properties
        FROM rvbbit.kg_edges e
        WHERE dir IN ('in', 'both')
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
    match_threshold double precision DEFAULT 0.92
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
BEGIN
    max_d := greatest(COALESCE(max_depth, 3), 0);
    dir := lower(COALESCE(direction, 'out'));
    IF dir NOT IN ('out', 'in', 'both') THEN
        RAISE EXCEPTION 'rvbbit.kg_paths: direction must be out, in, or both';
    END IF;
    IF max_d = 0 THEN
        RETURN;
    END IF;

    SELECT r.node_id, r.label INTO start_id, start_label
    FROM rvbbit.kg_resolve_node(subject_kind, subject_label, specialist, match_threshold) r
    ORDER BY r.score DESC, r.node_id
    LIMIT 1;
    SELECT r.node_id INTO target_id
    FROM rvbbit.kg_resolve_node(object_kind, object_label, specialist, match_threshold) r
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
        UNION ALL
        SELECT e.edge_id, e.object_node_id AS src, e.subject_node_id AS dst
        FROM rvbbit.kg_edges e
        WHERE dir IN ('in', 'both')
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
