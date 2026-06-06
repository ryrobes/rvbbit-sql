//! First-class triple extraction surface.
//!
//! `rvbbit.triples` itself is a seeded semantic operator so users can edit
//! the prompt/model/retry plan just like any other operator. This module adds
//! the stable SQL primitives around it: output validation, rowset shaping, and
//! ingestion into the native KG tables.

use pgrx::extension_sql;

extension_sql!(
    r#"
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
        op_max_tokens => 12000,
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

-- ── Row-context triple extraction ──────────────────────────────────────
-- Flatten a whole row (as jsonb) into ONE natural-language doc, then extract
-- entity/relationship triples from it. This is the substrate for data_crawl():
-- arbitrary user tables have no inherent "text column", so we synthesize one
-- from the row's own fields, dropping low-signal cells (ids/urls/pure numbers/
-- near-empty) so the LLM sees only descriptive content. The per-field 600-char
-- clamp bounds prompt size on wide rows.
CREATE OR REPLACE FUNCTION rvbbit.triples_row(
    row_doc jsonb,
    focus text DEFAULT 'all',
    opts jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE (subject text, predicate text, object text)
LANGUAGE sql
STABLE
AS $$
    SELECT t.subject, t.predicate, t.object
    FROM rvbbit.triples_json_rows(
        rvbbit.triples(
            (SELECT string_agg(key || ': ' || left(value, 600), E'\n' ORDER BY key)
               FROM jsonb_each_text(row_doc)
              WHERE value IS NOT NULL
                AND length(btrim(value)) >= 3
                AND value !~ '^[-+]?[0-9]+(\.[0-9]+)?$'
                AND value !~* '^https?://'
                AND lower(key) !~ '(_id$|^id$|uuid|guid|url)'),
            COALESCE(focus, 'all'), COALESCE(opts, '{}'::jsonb))) t;
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
	"#,
    name = "triples_bootstrap",
    requires = ["kg_bootstrap"],
);
