-- 0284: append-only human correction overlays for topology proposal bundles.
--
-- The signed excavation receipt remains untouched. A correction is a complete
-- overlay snapshot at one revision; the effective debugger tree is derived
-- from the immutable receipt plus the latest overlay. "complete" means only
-- that a reviewer finished this correction pass. It is not acceptance and it
-- never materializes governed topology.

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_bundle_corrections (
    correction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bundle_id uuid NOT NULL
        REFERENCES rvbbit.business_topology_proposal_bundles(bundle_id)
        ON DELETE RESTRICT,
    revision integer NOT NULL,
    correction_state text NOT NULL DEFAULT 'draft',
    correction jsonb NOT NULL,
    corrected_by text NOT NULL DEFAULT current_user,
    corrected_at timestamptz NOT NULL DEFAULT now(),
    previous_correction_id uuid
        REFERENCES rvbbit.business_topology_bundle_corrections(correction_id)
        ON DELETE RESTRICT,
    CONSTRAINT business_topology_bundle_corrections_revision_check
        CHECK (revision > 0),
    CONSTRAINT business_topology_bundle_corrections_state_check
        CHECK (correction_state IN ('draft','complete')),
    CONSTRAINT business_topology_bundle_corrections_json_check
        CHECK (
            jsonb_typeof(correction)='object'
            AND correction->>'schema_version'
                ='rvbbit.business-topology.bundle-correction.v1'
        ),
    CONSTRAINT business_topology_bundle_corrections_bundle_revision_key
        UNIQUE (bundle_id,revision)
);

CREATE INDEX IF NOT EXISTS business_topology_bundle_corrections_latest_idx
    ON rvbbit.business_topology_bundle_corrections
       (bundle_id,revision DESC,corrected_at DESC);

COMMENT ON TABLE rvbbit.business_topology_bundle_corrections IS
    'Versioned human correction overlays for immutable topology bundle receipts. A complete correction is still shadow review state, never governed topology.';

CREATE OR REPLACE FUNCTION rvbbit._business_topology_validate_bundle_correction(
    p_bundle_id uuid,
    p_correction jsonb
) RETURNS void
LANGUAGE plpgsql STABLE
AS $fn$
DECLARE
    v_result jsonb;
BEGIN
    SELECT receipt->'result' INTO v_result
      FROM rvbbit.business_topology_proposal_bundles
     WHERE bundle_id=p_bundle_id
       AND work_kind='neighborhood_synthesis';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'editable topology proposal bundle % was not found',p_bundle_id;
    END IF;
    IF p_correction IS NULL OR jsonb_typeof(p_correction)<>'object'
       OR p_correction->>'schema_version'
          <>'rvbbit.business-topology.bundle-correction.v1' THEN
        RAISE EXCEPTION 'bundle correction requires the v1 correction object';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_object_keys(p_correction) key
         WHERE key NOT IN (
             'schema_version','canonical_name','node_patches','binding_patches',
             'relationship_suggestions','review_note'
         )
    ) THEN
        RAISE EXCEPTION 'bundle correction contains an unsupported top-level key';
    END IF;
    IF p_correction ? 'canonical_name' AND (
        jsonb_typeof(p_correction->'canonical_name')<>'string'
        OR nullif(btrim(p_correction->>'canonical_name'),'') IS NULL
        OR length(p_correction->>'canonical_name')>120
    ) THEN
        RAISE EXCEPTION 'corrected canonical_name must contain 1 to 120 characters';
    END IF;
    IF p_correction ? 'review_note' AND (
        jsonb_typeof(p_correction->'review_note')<>'string'
        OR length(p_correction->>'review_note')>4000
    ) THEN
        RAISE EXCEPTION 'bundle correction review_note exceeds 4000 characters';
    END IF;
    IF jsonb_typeof(coalesce(p_correction->'node_patches','[]'::jsonb))<>'array'
       OR jsonb_typeof(coalesce(p_correction->'binding_patches','[]'::jsonb))<>'array'
       OR jsonb_typeof(coalesce(p_correction->'relationship_suggestions','[]'::jsonb))<>'array' THEN
        RAISE EXCEPTION 'bundle correction patch collections must be arrays';
    END IF;
    IF rvbbit._business_topology_bundle_has_private_keys(p_correction) THEN
        RAISE EXCEPTION 'bundle correction contains a forbidden value, fingerprint, or SQL key';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(coalesce(p_correction->'node_patches','[]'::jsonb)) patch
         WHERE jsonb_typeof(patch)<>'object'
            OR nullif(patch->>'node_key','') IS NULL
            OR EXISTS (
                SELECT 1 FROM jsonb_object_keys(patch) key
                 WHERE key NOT IN (
                     'node_key','name','description','node_kind',
                     'parent_node_key','suppressed'
                 )
            )
    ) THEN
        RAISE EXCEPTION 'node patches require a node_key and supported patch keys';
    END IF;
    IF EXISTS (
        SELECT patch->>'node_key'
          FROM jsonb_array_elements(coalesce(p_correction->'node_patches','[]'::jsonb)) patch
         GROUP BY patch->>'node_key' HAVING count(*)>1
    ) THEN
        RAISE EXCEPTION 'bundle correction contains duplicate node patches';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(coalesce(p_correction->'node_patches','[]'::jsonb)) patch
         WHERE NOT EXISTS (
             SELECT 1 FROM jsonb_array_elements(v_result->'nodes') node
              WHERE node->>'node_key'=patch->>'node_key'
         )
    ) THEN
        RAISE EXCEPTION 'node patch references a node outside the immutable receipt';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(coalesce(p_correction->'node_patches','[]'::jsonb)) patch
         WHERE (patch ? 'name' AND (
                    jsonb_typeof(patch->'name')<>'string'
                    OR nullif(btrim(patch->>'name'),'') IS NULL
                    OR length(patch->>'name')>180
               ))
            OR (patch ? 'description' AND patch->'description'<>'null'::jsonb AND (
                    jsonb_typeof(patch->'description')<>'string'
                    OR length(patch->>'description')>4000
               ))
            OR (patch ? 'node_kind' AND patch->>'node_kind' NOT IN (
                    'object','facet','lifecycle','event','measure','category'
               ))
            OR (patch ? 'suppressed' AND jsonb_typeof(patch->'suppressed')<>'boolean')
            OR (patch ? 'parent_node_key' AND patch->'parent_node_key'<>'null'::jsonb AND (
                    jsonb_typeof(patch->'parent_node_key')<>'string'
                    OR patch->>'parent_node_key'=patch->>'node_key'
                    OR NOT EXISTS (
                        SELECT 1 FROM jsonb_array_elements(v_result->'nodes') parent_node
                         WHERE parent_node->>'node_key'=patch->>'parent_node_key'
                    )
               ))
    ) THEN
        RAISE EXCEPTION 'node patch has an invalid name, type, parent, description, or suppression flag';
    END IF;

    IF EXISTS (
        WITH RECURSIVE original_nodes AS (
            SELECT node->>'node_key' AS node_key,node->>'parent_node_key' AS parent_node_key
              FROM jsonb_array_elements(v_result->'nodes') node
        ), patches AS (
            SELECT patch->>'node_key' AS node_key,patch
              FROM jsonb_array_elements(coalesce(p_correction->'node_patches','[]'::jsonb)) patch
        ), effective AS (
            SELECT original.node_key,
                   CASE WHEN patches.patch ? 'parent_node_key'
                        THEN patches.patch->>'parent_node_key'
                        ELSE original.parent_node_key END AS parent_node_key,
                   CASE WHEN patches.patch ? 'suppressed'
                        THEN (patches.patch->>'suppressed')::boolean
                        ELSE false END AS suppressed
              FROM original_nodes original
              LEFT JOIN patches USING (node_key)
        ), walk(start_key,current_key,path,cycle) AS (
            SELECT node_key,parent_node_key,ARRAY[node_key],false
              FROM effective
             WHERE NOT suppressed AND parent_node_key IS NOT NULL
            UNION ALL
            SELECT walk.start_key,parent.parent_node_key,
                   walk.path||parent.node_key,parent.node_key=ANY(walk.path)
              FROM walk
              JOIN effective parent ON parent.node_key=walk.current_key
             WHERE NOT walk.cycle AND NOT parent.suppressed
        )
        SELECT 1 FROM walk WHERE cycle
    ) THEN
        RAISE EXCEPTION 'corrected node hierarchy contains a cycle';
    END IF;
    IF EXISTS (
        WITH original_nodes AS (
            SELECT node->>'node_key' AS node_key,node->>'parent_node_key' AS parent_node_key
              FROM jsonb_array_elements(v_result->'nodes') node
        ), patches AS (
            SELECT patch->>'node_key' AS node_key,patch
              FROM jsonb_array_elements(coalesce(p_correction->'node_patches','[]'::jsonb)) patch
        ), effective AS (
            SELECT original.node_key,
                   CASE WHEN patches.patch ? 'parent_node_key'
                        THEN patches.patch->>'parent_node_key'
                        ELSE original.parent_node_key END AS parent_node_key,
                   CASE WHEN patches.patch ? 'suppressed'
                        THEN (patches.patch->>'suppressed')::boolean
                        ELSE false END AS suppressed
              FROM original_nodes original
              LEFT JOIN patches USING (node_key)
        )
        SELECT 1
          FROM effective child
          JOIN effective parent ON parent.node_key=child.parent_node_key
         WHERE NOT child.suppressed AND parent.suppressed
    ) THEN
        RAISE EXCEPTION 'a visible corrected node cannot have a suppressed parent';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(coalesce(p_correction->'binding_patches','[]'::jsonb)) patch
         WHERE jsonb_typeof(patch)<>'object'
            OR nullif(patch->>'node_key','') IS NULL
            OR nullif(patch->>'population_id','') IS NULL
            OR nullif(patch->>'original_binding_role','') IS NULL
            OR EXISTS (
                SELECT 1 FROM jsonb_object_keys(patch) key
                 WHERE key NOT IN (
                     'node_key','population_id','original_binding_role',
                     'binding_role','authority_hint','suppressed'
                 )
            )
    ) THEN
        RAISE EXCEPTION 'binding patches require their original binding identity';
    END IF;
    IF EXISTS (
        SELECT patch->>'node_key',patch->>'population_id',patch->>'original_binding_role'
          FROM jsonb_array_elements(coalesce(p_correction->'binding_patches','[]'::jsonb)) patch
         GROUP BY 1,2,3 HAVING count(*)>1
    ) THEN
        RAISE EXCEPTION 'bundle correction contains duplicate binding patches';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(coalesce(p_correction->'binding_patches','[]'::jsonb)) patch
         WHERE NOT EXISTS (
             SELECT 1 FROM jsonb_array_elements(v_result->'bindings') binding
              WHERE binding->>'node_key'=patch->>'node_key'
                AND binding->>'population_id'=patch->>'population_id'
                AND binding->>'binding_role'=patch->>'original_binding_role'
         )
            OR (patch ? 'binding_role' AND patch->>'binding_role' NOT IN (
                'identity','attribute','event','measure','category','status',
                'time','geography','evidence','context'
            ))
            OR (patch ? 'authority_hint' AND patch->>'authority_hint' NOT IN (
                'unknown','primary','secondary','derived','conflicting'
            ))
            OR (patch ? 'suppressed' AND jsonb_typeof(patch->'suppressed')<>'boolean')
    ) THEN
        RAISE EXCEPTION 'binding patch is outside the receipt or has an invalid role, authority, or suppression flag';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(coalesce(p_correction->'relationship_suggestions','[]'::jsonb)) suggestion
         WHERE jsonb_typeof(suggestion)<>'object'
            OR suggestion->>'kind' NOT IN (
                'split_node','merge_nodes','reconsider_relationship','other'
            )
            OR jsonb_typeof(suggestion->'node_keys')<>'array'
            OR jsonb_array_length(suggestion->'node_keys')=0
            OR (suggestion->>'kind'='split_node' AND jsonb_array_length(suggestion->'node_keys')<>1)
            OR (suggestion->>'kind'='merge_nodes' AND jsonb_array_length(suggestion->'node_keys')<2)
            OR (suggestion ? 'note' AND (
                jsonb_typeof(suggestion->'note')<>'string'
                OR length(suggestion->>'note')>2000
            ))
            OR EXISTS (
                SELECT 1 FROM jsonb_object_keys(suggestion) key
                 WHERE key NOT IN ('kind','node_keys','note')
            )
            OR EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(suggestion->'node_keys') node_key
                 WHERE NOT EXISTS (
                     SELECT 1 FROM jsonb_array_elements(v_result->'nodes') node
                      WHERE node->>'node_key'=node_key
                 )
            )
    ) THEN
        RAISE EXCEPTION 'relationship suggestion has invalid nodes, kind, or note';
    END IF;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_save_bundle_correction(
    p_bundle_id uuid,
    p_correction jsonb,
    p_correction_state text DEFAULT 'draft',
    p_corrected_by text DEFAULT NULL,
    p_expected_revision integer DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_current_revision integer;
    v_previous_correction_id uuid;
    v_correction_id uuid;
    v_corrected_by text := coalesce(nullif(btrim(p_corrected_by),''),current_user);
BEGIN
    IF p_correction_state NOT IN ('draft','complete') THEN
        RAISE EXCEPTION 'correction state must be draft or complete';
    END IF;
    PERFORM 1
      FROM rvbbit.business_topology_proposal_bundles
     WHERE bundle_id=p_bundle_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'topology proposal bundle % was not found',p_bundle_id;
    END IF;
    PERFORM rvbbit._business_topology_validate_bundle_correction(
        p_bundle_id,p_correction
    );
    SELECT revision,correction_id
      INTO v_current_revision,v_previous_correction_id
      FROM rvbbit.business_topology_bundle_corrections
     WHERE bundle_id=p_bundle_id
     ORDER BY revision DESC
     LIMIT 1;
    v_current_revision := coalesce(v_current_revision,0);
    IF p_expected_revision IS NOT NULL
       AND p_expected_revision<>v_current_revision THEN
        RAISE EXCEPTION
            'bundle correction changed from expected revision % to %; refresh before saving',
            p_expected_revision,v_current_revision;
    END IF;
    INSERT INTO rvbbit.business_topology_bundle_corrections (
        bundle_id,revision,correction_state,correction,corrected_by,
        previous_correction_id
    ) VALUES (
        p_bundle_id,v_current_revision+1,p_correction_state,p_correction,
        v_corrected_by,v_previous_correction_id
    ) RETURNING correction_id INTO v_correction_id;
    RETURN jsonb_build_object(
        'correction_id',v_correction_id,
        'bundle_id',p_bundle_id,
        'revision',v_current_revision+1,
        'correction_state',p_correction_state,
        'materialized_topology',false
    );
END
$fn$;

COMMENT ON FUNCTION rvbbit.business_topology_save_bundle_correction(
    uuid,jsonb,text,text,integer
) IS
    'Appends an optimistic-concurrency-protected correction snapshot. It never changes the source receipt or materializes governed topology.';

CREATE OR REPLACE VIEW rvbbit.business_topology_proposal_bundle_review AS
SELECT summary.*,
       coalesce(latest.revision,0) AS correction_revision,
       latest.correction_id,
       latest.correction_state,
       latest.correction,
       latest.corrected_by,
       latest.corrected_at
  FROM rvbbit.business_topology_proposal_bundle_summary summary
  LEFT JOIN LATERAL (
      SELECT correction.correction_id,correction.revision,
             correction.correction_state,correction.correction,
             correction.corrected_by,correction.corrected_at
        FROM rvbbit.business_topology_bundle_corrections correction
       WHERE correction.bundle_id=summary.bundle_id
       ORDER BY correction.revision DESC
       LIMIT 1
  ) latest ON true;

COMMENT ON VIEW rvbbit.business_topology_proposal_bundle_review IS
    'DataRabbit review projection combining immutable proposal receipts with only the latest versioned human correction overlay.';
