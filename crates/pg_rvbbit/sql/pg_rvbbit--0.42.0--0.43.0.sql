-- Knowledge graph entity-resolution review and merge primitives.

CREATE TABLE IF NOT EXISTS rvbbit.kg_merge_candidates (
    candidate_id   bigserial PRIMARY KEY,
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
    CONSTRAINT kg_merge_candidates_pair_method_unique UNIQUE (left_node_id, right_node_id, method)
);

CREATE TABLE IF NOT EXISTS rvbbit.kg_node_merges (
    merge_id          bigserial PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS kg_merge_candidates_left_idx ON rvbbit.kg_merge_candidates(left_node_id);
CREATE INDEX IF NOT EXISTS kg_merge_candidates_right_idx ON rvbbit.kg_merge_candidates(right_node_id);
CREATE INDEX IF NOT EXISTS kg_merge_candidates_status_idx ON rvbbit.kg_merge_candidates(status, score DESC);
CREATE INDEX IF NOT EXISTS kg_merge_candidates_query_id_idx ON rvbbit.kg_merge_candidates(query_id);
CREATE INDEX IF NOT EXISTS kg_node_merges_winner_idx ON rvbbit.kg_node_merges(winner_node_id);
CREATE INDEX IF NOT EXISTS kg_node_merges_loser_idx ON rvbbit.kg_node_merges(loser_node_id);
CREATE INDEX IF NOT EXISTS kg_node_merges_query_id_idx ON rvbbit.kg_node_merges(query_id);

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

CREATE OR REPLACE FUNCTION rvbbit.kg_suggest_merges(
    node_kind text DEFAULT NULL,
    threshold double precision DEFAULT 0.86,
    limit_count int DEFAULT 1000
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
          ON n1.kind = n2.kind
         AND n1.node_id < n2.node_id
        WHERE (norm_kind IS NULL OR n1.kind = norm_kind)
          AND NOT EXISTS (
              SELECT 1
              FROM rvbbit.kg_merge_candidates c
              WHERE c.left_node_id = n1.node_id
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
            query_id, left_node_id, right_node_id, kind, score, method, reason, status, properties
        )
        SELECT qid,
               p.left_id,
               p.right_id,
               p.node_kind,
               p.pair_score,
               'label_similarity',
               format('label similarity %s between "%s" and "%s"', round(p.pair_score::numeric, 3), p.left_name, p.right_name),
               'pending',
               jsonb_build_object('left_label', p.left_name, 'right_label', p.right_name)
        FROM picked p
        ON CONFLICT ON CONSTRAINT kg_merge_candidates_pair_method_unique DO UPDATE SET
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

    INSERT INTO rvbbit.kg_node_merges(
        query_id, candidate_id, winner_node_id, loser_node_id,
        loser_kind, loser_label, loser_label_norm, loser_properties, properties
    )
    VALUES (
        qid, merge_candidate_id, winner.node_id, loser.node_id,
        loser.kind, loser.label, loser.label_norm, loser.properties, props
    )
    RETURNING merge_id INTO out_merge_id;

    UPDATE rvbbit.kg_nodes
    SET properties = loser.properties || winner.properties || props,
        confidence = greatest(winner.confidence, loser.confidence)
    WHERE node_id = winner.node_id;

    INSERT INTO rvbbit.kg_aliases(node_id, kind, alias, alias_norm, confidence, properties)
    SELECT winner.node_id,
           kind,
           alias,
           alias_norm,
           confidence,
           properties || jsonb_build_object('merged_from_node_id', loser.node_id)
    FROM rvbbit.kg_aliases
    WHERE node_id = loser.node_id
    ON CONFLICT (kind, alias_norm) DO UPDATE SET
        node_id = EXCLUDED.node_id,
        alias = EXCLUDED.alias,
        confidence = greatest(rvbbit.kg_aliases.confidence, EXCLUDED.confidence),
        properties = rvbbit.kg_aliases.properties || EXCLUDED.properties;

    PERFORM rvbbit.kg_assert_alias(
        winner.node_id,
        loser.label,
        loser.confidence,
        jsonb_build_object('merged_from_node_id', loser.node_id)
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
