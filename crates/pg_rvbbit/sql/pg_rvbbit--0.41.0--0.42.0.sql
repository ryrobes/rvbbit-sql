-- Query-level provenance for KG evidence and semantic receipts.

CREATE OR REPLACE FUNCTION rvbbit.current_query_id()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    raw_query_id text;
    next_query_id uuid;
BEGIN
    raw_query_id := NULLIF(current_setting('rvbbit.query_id', true), '');
    IF raw_query_id IS NOT NULL THEN
        BEGIN
            RETURN raw_query_id::uuid;
        EXCEPTION WHEN OTHERS THEN
            -- A bad manually-set value should not poison the session.
            NULL;
        END;
    END IF;

    next_query_id := gen_random_uuid();
    PERFORM set_config('rvbbit.query_id', next_query_id::text, false);
    RETURN next_query_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.reset_query_id()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    next_query_id uuid := gen_random_uuid();
BEGIN
    PERFORM set_config('rvbbit.query_id', next_query_id::text, false);
    RETURN next_query_id;
END $$;

ALTER TABLE rvbbit.kg_evidence
    ADD COLUMN IF NOT EXISTS query_id uuid;

CREATE INDEX IF NOT EXISTS kg_evidence_query_id_idx ON rvbbit.kg_evidence(query_id);

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
    evidence_props jsonb;
    raw_query_id text;
    resolved_query_id uuid;
BEGIN
    IF target_edge_id IS NULL AND target_node_id IS NULL THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: target_edge_id or target_node_id is required';
    END IF;
    IF confidence < 0.0 OR confidence > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_link_evidence: confidence must be between 0 and 1';
    END IF;

    evidence_props := COALESCE(properties, '{}'::jsonb);
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
        edge_id, node_id, query_id, source_table, source_pk, source_column,
        evidence_text, span, confidence, properties
    )
    VALUES (
        target_edge_id, target_node_id, resolved_query_id, source_table, source_pk, source_column,
        evidence_text, span, confidence, evidence_props
    )
    RETURNING evidence_id INTO out_evidence_id;

    RETURN out_evidence_id;
END $$;
