-- 0267: Clover's hosted LLM is a managed service, not a promise that a
-- particular upstream model is running forever. New clients use the stable
-- `clover` wire id. The previously advertised `gemma4` id remains an explicit
-- compatibility alias at Hutch and in the client transport policy.

UPDATE rvbbit.backends
SET transport_opts = coalesce(transport_opts, '{}'::jsonb) || jsonb_build_object(
        'model', 'clover',
        'model_policy', 'managed',
        'model_aliases', jsonb_build_array('gemma4')
    ),
    source_provider = 'rvbbit.ai',
    source_model = 'Clover Hosted',
    source_revision = 'managed-current'
WHERE name = 'clover_llm'
  AND (
      install_manifest->>'capability' = 'managed/clover'
      OR endpoint_url LIKE '%clover.rvbb.it%/v1/chat/completions'
  );

-- Normalize existing operator nodes that were generated from the old managed
-- catalog. This is deliberately provider-scoped: a separately registered
-- local model named gemma4 remains a legitimate, selectable model.
WITH rewritten AS (
    SELECT o.name,
           jsonb_agg(
               CASE
                   WHEN node->>'provider' = 'clover_llm'
                    AND node->>'model' = 'gemma4'
                   THEN jsonb_set(node, '{model}', to_jsonb('clover'::text))
                   ELSE node
               END
               ORDER BY ord
           ) AS steps
    FROM rvbbit.operators o
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(o.steps) = 'array' THEN o.steps ELSE '[]'::jsonb END
    ) WITH ORDINALITY AS n(node, ord)
    GROUP BY o.name
)
UPDATE rvbbit.operators o
SET steps = rewritten.steps,
    model = CASE WHEN o.model = 'gemma4' THEN 'clover' ELSE o.model END,
    updated_at = clock_timestamp()
FROM rewritten
WHERE o.name = rewritten.name
  AND o.steps IS DISTINCT FROM rewritten.steps;

UPDATE rvbbit.cost_policies
SET model = 'clover',
    notes = 'Clover included value: managed hosted intelligence covered by subscription, never billed a la carte',
    updated_at = clock_timestamp()
WHERE target_kind = 'backend'
  AND target_name = 'clover_llm';

-- Rebuild the picker-visible row after changing the backend's public alias;
-- this also prunes the old capability-owned gemma4 row.
SELECT rvbbit.sync_managed_provider_models();
