DROP FUNCTION IF EXISTS rvbbit.kg_context(text, text, int, int, text, boolean, text, double precision);

CREATE OR REPLACE FUNCTION rvbbit.kg_context(
    node_kind text,
    node_label text,
    max_depth int DEFAULT 2,
    max_edges int DEFAULT 100,
    direction text DEFAULT 'both',
    include_evidence boolean DEFAULT true,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92
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
BEGIN
    max_d := greatest(COALESCE(max_depth, 2), 0);
    max_rows := greatest(COALESCE(max_edges, 100), 1);
    dir := lower(COALESCE(direction, 'both'));
    IF dir NOT IN ('out', 'in', 'both') THEN
        RAISE EXCEPTION 'rvbbit.kg_context: direction must be out, in, or both';
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
        SELECT e.edge_id AS a_edge_id,
               e.subject_node_id AS a_from_node_id,
               e.object_node_id AS a_to_node_id,
               'out'::text AS a_edge_direction,
               e.predicate AS a_predicate,
               e.confidence AS a_edge_confidence,
               e.properties AS a_edge_properties
        FROM rvbbit.kg_edges e
        WHERE dir IN ('out', 'both')
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
               (w.w_score * a.a_edge_confidence * 0.85)::double precision AS w_score
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
          AND (
              ev_row.edge_id = ranked.r_edge_id
              OR ev_row.node_id = ranked.r_to_node_id
          )
    ) ev
    ORDER BY ranked.r_context_rank;
END $$;
