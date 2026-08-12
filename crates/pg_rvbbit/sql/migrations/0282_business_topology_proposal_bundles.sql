-- 0282: receipt-backed Business Topology proposal bundles.
--
-- A validated excavation skeleton is an internally consistent proposal: its
-- bindings, hierarchy, and edges refer to local node keys that must be
-- materialized together.  Splitting that skeleton into the existing atomic
-- proposal ledger would either lose those references or require accepting
-- nodes before a reviewer has accepted the whole idea.  Keep the complete,
-- immutable-receipt-backed result together until a transactional promotion
-- contract is implemented.
--
-- Deliberately, this migration does not provide an "accepted" state and does
-- not write business_topology_nodes, bindings, edges, or populations.

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_proposal_bundles (
    bundle_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bundle_key text NOT NULL UNIQUE,
    plan_sha256 text NOT NULL,
    work_id text NOT NULL,
    work_kind text NOT NULL,
    scope_kind text NOT NULL,
    scope_id text NOT NULL,
    source_keys text[] NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    receipt jsonb NOT NULL,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_versions text[] NOT NULL DEFAULT '{}'::text[],
    proposed_by text NOT NULL DEFAULT current_user,
    proposed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by text,
    reviewed_at timestamptz,
    review_reason text,
    supersedes uuid
        REFERENCES rvbbit.business_topology_proposal_bundles(bundle_id)
        ON DELETE SET NULL,
    CONSTRAINT business_topology_proposal_bundles_plan_hash_check CHECK (
        plan_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT business_topology_proposal_bundles_work_kind_check CHECK (
        work_kind IN ('neighborhood_synthesis','bridge_synthesis')
    ),
    CONSTRAINT business_topology_proposal_bundles_scope_kind_check CHECK (
        (work_kind='neighborhood_synthesis' AND scope_kind='excavation_unit')
        OR (work_kind='bridge_synthesis' AND scope_kind='boundary_link')
    ),
    CONSTRAINT business_topology_proposal_bundles_status_check CHECK (
        status IN ('proposed','needs_revision','rejected','superseded')
    ),
    CONSTRAINT business_topology_proposal_bundles_sources_check CHECK (
        cardinality(source_keys) > 0
    ),
    CONSTRAINT business_topology_proposal_bundles_json_check CHECK (
        jsonb_typeof(receipt)='object' AND jsonb_typeof(context)='object'
    ),
    CONSTRAINT business_topology_proposal_bundles_receipt_identity_check CHECK (
        receipt->>'schema_version'='rvbbit.business-topology.work-receipt.v1'
        AND receipt->>'plan_sha256'=plan_sha256
        AND receipt->>'work_id'=work_id
        AND receipt->>'work_kind'=work_kind
    ),
    CONSTRAINT business_topology_proposal_bundles_plan_work_key
        UNIQUE (plan_sha256,work_id)
);

CREATE INDEX IF NOT EXISTS business_topology_proposal_bundles_inbox_idx
    ON rvbbit.business_topology_proposal_bundles
       (status,updated_at DESC,proposed_at DESC);
CREATE INDEX IF NOT EXISTS business_topology_proposal_bundles_scope_idx
    ON rvbbit.business_topology_proposal_bundles
       (scope_kind,scope_id,updated_at DESC);

COMMENT ON TABLE rvbbit.business_topology_proposal_bundles IS
    'Validated, receipt-backed shadow skeletons kept internally consistent for review. Bundles are not governed topology and cannot be accepted or materialized by this contract.';

CREATE OR REPLACE FUNCTION rvbbit._business_topology_bundle_has_private_keys(
    p_value jsonb
) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
AS $fn$
DECLARE
    v_key text;
    v_child jsonb;
BEGIN
    IF p_value IS NULL THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(p_value)='object' THEN
        FOR v_key,v_child IN SELECT key,value FROM jsonb_each(p_value)
        LOOP
            IF v_key IN (
                'values','raw_sample','raw_samples','sample_values',
                'fingerprint','fingerprints','value_fingerprints',
                'value_fingerprint_signature','sql','where_sql'
            ) THEN
                RETURN true;
            END IF;
            IF v_key IN ('raw_values','value_hashes') AND v_child <> 'false'::jsonb THEN
                RETURN true;
            END IF;
            IF rvbbit._business_topology_bundle_has_private_keys(v_child) THEN
                RETURN true;
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(p_value)='array' THEN
        FOR v_child IN SELECT value FROM jsonb_array_elements(p_value)
        LOOP
            IF rvbbit._business_topology_bundle_has_private_keys(v_child) THEN
                RETURN true;
            END IF;
        END LOOP;
    END IF;
    RETURN false;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_stage_proposal_bundle(
    p_receipt jsonb,
    p_scope_kind text,
    p_scope_id text,
    p_source_keys text[],
    p_context jsonb DEFAULT '{}'::jsonb,
    p_proposed_by text DEFAULT NULL,
    p_supersedes uuid DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_bundle_id uuid;
    v_plan_sha256 text;
    v_work_id text;
    v_work_kind text;
    v_result jsonb;
    v_validation jsonb;
    v_expected_result_version text;
    v_expected_scope_kind text;
    v_model_versions text[];
BEGIN
    IF p_receipt IS NULL OR jsonb_typeof(p_receipt)<>'object' THEN
        RAISE EXCEPTION 'proposal bundle receipt must be a JSON object';
    END IF;
    IF p_receipt->>'schema_version'<>'rvbbit.business-topology.work-receipt.v1' THEN
        RAISE EXCEPTION 'proposal bundle requires a v1 work receipt';
    END IF;
    v_plan_sha256 := nullif(p_receipt->>'plan_sha256','');
    v_work_id := nullif(p_receipt->>'work_id','');
    v_work_kind := nullif(p_receipt->>'work_kind','');
    v_result := p_receipt->'result';
    v_validation := p_receipt->'validation';
    IF v_plan_sha256 IS NULL OR v_plan_sha256 !~ '^[0-9a-f]{64}$'
       OR v_work_id IS NULL THEN
        RAISE EXCEPTION 'proposal bundle receipt has an invalid plan or work identity';
    END IF;
    v_expected_result_version := CASE v_work_kind
        WHEN 'neighborhood_synthesis'
            THEN 'rvbbit.business-topology.neighborhood-skeleton-result.v1'
        WHEN 'bridge_synthesis'
            THEN 'rvbbit.business-topology.bridge-result.v1'
        ELSE NULL
    END;
    v_expected_scope_kind := CASE v_work_kind
        WHEN 'neighborhood_synthesis' THEN 'excavation_unit'
        WHEN 'bridge_synthesis' THEN 'boundary_link'
        ELSE NULL
    END;
    IF v_expected_result_version IS NULL THEN
        RAISE EXCEPTION 'only synthesis results can be staged as proposal bundles';
    END IF;
    IF p_scope_kind <> v_expected_scope_kind
       OR nullif(btrim(p_scope_id),'') IS NULL THEN
        RAISE EXCEPTION 'proposal bundle scope does not match its work kind';
    END IF;
    IF coalesce(cardinality(p_source_keys),0)=0
       OR EXISTS (SELECT 1 FROM unnest(p_source_keys) key WHERE nullif(btrim(key),'') IS NULL)
       OR (SELECT count(*) FROM unnest(p_source_keys) key)
          <> (SELECT count(DISTINCT key) FROM unnest(p_source_keys) key) THEN
        RAISE EXCEPTION 'proposal bundle requires distinct non-empty source keys';
    END IF;
    IF p_context IS NULL OR jsonb_typeof(p_context)<>'object' THEN
        RAISE EXCEPTION 'proposal bundle context must be a JSON object';
    END IF;
    IF v_result IS NULL OR jsonb_typeof(v_result)<>'object'
       OR v_result->>'schema_version'<>v_expected_result_version
       OR v_result->>'work_id'<>v_work_id
       OR v_result->>'status'<>'proposed' THEN
        RAISE EXCEPTION 'proposal bundle result does not match its receipt contract';
    END IF;
    IF (v_work_kind='neighborhood_synthesis' AND (
            jsonb_typeof(v_result->'nodes')<>'array'
            OR jsonb_typeof(v_result->'bindings')<>'array'
            OR jsonb_typeof(v_result->'edges')<>'array'
            OR jsonb_typeof(v_result->'unbound_population_ids')<>'array'
        ))
       OR (v_work_kind='bridge_synthesis'
           AND jsonb_typeof(v_result->'findings')<>'array') THEN
        RAISE EXCEPTION 'proposal bundle result arrays do not match its work kind';
    END IF;
    IF v_validation IS NULL OR jsonb_typeof(v_validation)<>'object'
       OR v_validation->>'valid'<>'true'
       OR v_validation->>'work_id'<>v_work_id
       OR v_validation->>'work_kind'<>v_work_kind THEN
        RAISE EXCEPTION 'proposal bundle requires a matching successful validation';
    END IF;
    IF jsonb_typeof(coalesce(p_receipt->'execution_receipts','null'::jsonb))<>'array' THEN
        RAISE EXCEPTION 'proposal bundle execution receipts must be an array';
    END IF;
    IF rvbbit._business_topology_bundle_has_private_keys(v_result)
       OR rvbbit._business_topology_bundle_has_private_keys(p_context) THEN
        RAISE EXCEPTION 'proposal bundle contains a forbidden value, fingerprint, or SQL key';
    END IF;

    SELECT coalesce(array_agg(DISTINCT model_version ORDER BY model_version),ARRAY[]::text[])
      INTO v_model_versions
      FROM (
          SELECT nullif(item->>'model_version','') AS model_version
            FROM jsonb_array_elements(p_receipt->'execution_receipts') item
      ) versions
     WHERE model_version IS NOT NULL;

    INSERT INTO rvbbit.business_topology_proposal_bundles (
        bundle_key,plan_sha256,work_id,work_kind,scope_kind,scope_id,
        source_keys,status,receipt,context,model_versions,proposed_by,
        proposed_at,updated_at,supersedes
    ) VALUES (
        'bundle:'||v_plan_sha256||':'||v_work_id,
        v_plan_sha256,v_work_id,v_work_kind,p_scope_kind,btrim(p_scope_id),
        p_source_keys,'proposed',p_receipt,p_context,v_model_versions,
        coalesce(nullif(btrim(p_proposed_by),''),current_user),
        now(),now(),p_supersedes
    )
    ON CONFLICT (plan_sha256,work_id) DO UPDATE SET
        scope_kind=EXCLUDED.scope_kind,
        scope_id=EXCLUDED.scope_id,
        source_keys=EXCLUDED.source_keys,
        status='proposed',
        receipt=EXCLUDED.receipt,
        context=EXCLUDED.context,
        model_versions=EXCLUDED.model_versions,
        proposed_by=EXCLUDED.proposed_by,
        proposed_at=now(),
        updated_at=now(),
        reviewed_by=NULL,
        reviewed_at=NULL,
        review_reason=NULL,
        supersedes=EXCLUDED.supersedes
    WHERE rvbbit.business_topology_proposal_bundles.status
          IN ('proposed','needs_revision')
    RETURNING bundle_id INTO v_bundle_id;

    IF v_bundle_id IS NULL THEN
        RAISE EXCEPTION 'a rejected or superseded proposal bundle cannot be overwritten';
    END IF;
    RETURN v_bundle_id;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_review_proposal_bundle(
    p_bundle_id uuid,
    p_decision text,
    p_reason text DEFAULT NULL,
    p_reviewer text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_old_status text;
    v_reviewer text := coalesce(nullif(btrim(p_reviewer),''),current_user);
BEGIN
    IF p_decision NOT IN ('proposed','needs_revision','rejected','superseded') THEN
        RAISE EXCEPTION
            'bundle decision must be proposed, needs_revision, rejected, or superseded; acceptance is not implemented';
    END IF;
    SELECT status INTO v_old_status
      FROM rvbbit.business_topology_proposal_bundles
     WHERE bundle_id=p_bundle_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'business topology proposal bundle % not found',p_bundle_id;
    END IF;
    IF v_old_status IN ('rejected','superseded') THEN
        RAISE EXCEPTION 'terminal proposal bundle % is already %',p_bundle_id,v_old_status;
    END IF;
    UPDATE rvbbit.business_topology_proposal_bundles
       SET status=p_decision,
           reviewed_by=CASE WHEN p_decision='proposed' THEN NULL ELSE v_reviewer END,
           reviewed_at=CASE WHEN p_decision='proposed' THEN NULL ELSE now() END,
           review_reason=CASE WHEN p_decision='proposed' THEN NULL
                              ELSE nullif(btrim(p_reason),'') END,
           updated_at=now()
     WHERE bundle_id=p_bundle_id;
    RETURN jsonb_build_object(
        'bundle_id',p_bundle_id,
        'status',p_decision,
        'materialized_topology',false
    );
END
$fn$;

CREATE OR REPLACE VIEW rvbbit.business_topology_proposal_bundle_summary AS
SELECT bundle.bundle_id,bundle.bundle_key,bundle.status,
       bundle.work_kind,bundle.scope_kind,bundle.scope_id,
       bundle.plan_sha256,bundle.work_id,
       cardinality(bundle.source_keys)::integer AS source_count,
       bundle.source_keys,
       bundle.context->'sources' AS sources,
       CASE WHEN bundle.work_kind='neighborhood_synthesis'
            THEN jsonb_array_length(bundle.receipt #> '{result,nodes}')
            ELSE 0 END AS node_count,
       CASE WHEN bundle.work_kind='neighborhood_synthesis'
            THEN jsonb_array_length(bundle.receipt #> '{result,bindings}')
            ELSE 0 END AS binding_count,
       CASE WHEN bundle.work_kind='neighborhood_synthesis'
            THEN jsonb_array_length(bundle.receipt #> '{result,edges}')
            ELSE 0 END AS edge_count,
       CASE WHEN bundle.work_kind='neighborhood_synthesis'
            THEN jsonb_array_length(bundle.receipt #> '{result,unbound_population_ids}')
            ELSE 0 END AS unbound_population_count,
       confidence.average_confidence,
       confidence.minimum_confidence,
       usage.prompt_tokens,usage.completion_tokens,usage.total_tokens,usage.cost,
       bundle.model_versions,
       bundle.receipt->>'worker_version' AS worker_version,
       bundle.receipt->>'prompt_contract_version' AS prompt_contract_version,
       bundle.receipt->'validation' AS validation,
       bundle.receipt->'result' AS result,
       bundle.context,bundle.proposed_by,bundle.proposed_at,bundle.updated_at,
       bundle.reviewed_by,bundle.reviewed_at,bundle.review_reason,
       bundle.supersedes,
       bundle.receipt->>'input_packet_sha256' AS input_packet_sha256,
       bundle.receipt->>'completed_at' AS completed_at,
       bundle.receipt->'execution_receipts' AS execution_receipts,
       CASE WHEN bundle.work_kind='bridge_synthesis'
            THEN jsonb_array_length(bundle.receipt #> '{result,findings}')
            ELSE 0 END AS finding_count
  FROM rvbbit.business_topology_proposal_bundles bundle
  LEFT JOIN LATERAL (
      SELECT avg((item->>'confidence')::double precision) AS average_confidence,
             min((item->>'confidence')::double precision) AS minimum_confidence
        FROM (
            SELECT item
              FROM jsonb_array_elements(
                  CASE WHEN bundle.work_kind='neighborhood_synthesis'
                       THEN bundle.receipt #> '{result,nodes}' ELSE '[]'::jsonb END
              ) item
            UNION ALL
            SELECT item
              FROM jsonb_array_elements(
                  CASE WHEN bundle.work_kind='neighborhood_synthesis'
                       THEN bundle.receipt #> '{result,bindings}' ELSE '[]'::jsonb END
              ) item
            UNION ALL
            SELECT item
              FROM jsonb_array_elements(bundle.receipt #> '{result,edges}') item
            UNION ALL
            SELECT item
              FROM jsonb_array_elements(
                  CASE WHEN bundle.work_kind='bridge_synthesis'
                       THEN bundle.receipt #> '{result,findings}' ELSE '[]'::jsonb END
              ) item
        ) confidence_items
  ) confidence ON true
  LEFT JOIN LATERAL (
      SELECT coalesce(sum((item #>> '{usage,prompt_tokens}')::bigint),0)::bigint
                 AS prompt_tokens,
             coalesce(sum((item #>> '{usage,completion_tokens}')::bigint),0)::bigint
                 AS completion_tokens,
             coalesce(sum((item #>> '{usage,total_tokens}')::bigint),0)::bigint
                 AS total_tokens,
             coalesce(sum((item #>> '{usage,cost}')::numeric),0)::numeric AS cost
        FROM jsonb_array_elements(bundle.receipt->'execution_receipts') item
  ) usage ON true;

COMMENT ON VIEW rvbbit.business_topology_proposal_bundle_summary IS
    'DataRabbit-facing review projection for ungoverned Business Topology skeleton bundles, including coverage, confidence, model, and cost receipts.';
