-- Wire built-in operators to use the registered GPU specialists.
-- Run AFTER docker/sql/register-gpu-specialists.sql.
--
--   make wire-specialists
--
-- This UPDATEs rvbbit.operators.steps to route through specialist HTTP
-- endpoints instead of LLM chat completions. Each call is faster + cheaper:
--
--   rvbbit.about(text, criterion)       LLM ~800ms / $0.0003   ->  rerank ~20ms / $0
--   rvbbit.extract(text, what)          LLM ~400ms / $0.0001   ->  GLiNER  ~5ms / $0
--
-- Repeats hit rvbbit.receipts (cross-backend cache) regardless of path.
-- Switch back to the LLM path by setting steps = NULL.

-- about → bge-reranker-v2-m3 cross-encoder (via Gradio).
-- about's second arg is named `topic` (not `criterion`) — the rerank
-- sidecar accepts {text, criterion}, so we map inputs.topic → criterion.
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'rerank',
        'kind', 'specialist',
        'specialist', 'rerank',
        'inputs', jsonb_build_object(
            'text', '{{ inputs.text }}',
            'criterion', '{{ inputs.topic }}'
        )
    )
)
WHERE name = 'about';

-- extract → GLiNER medium (native transport).
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'gliner',
        'kind', 'specialist',
        'specialist', 'extract',
        'inputs', jsonb_build_object(
            'text', '{{ inputs.text }}',
            'what', '{{ inputs.what }}'
        )
    )
)
WHERE name = 'extract';

-- classify → deberta-v3-large zero-shot (nli_classify endpoint).
-- Operator's arg_names are {text, categories}; specialist takes
-- {text, candidate_labels}.
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'nli',
        'kind', 'specialist',
        'specialist', 'nli_classify',
        'inputs', jsonb_build_object(
            'text', '{{ inputs.text }}',
            'candidate_labels', '{{ inputs.categories }}'
        )
    )
)
WHERE name = 'classify';

-- sentiment → nli_classify with a fixed 4-label set. The candidate
-- labels are baked into the step config (not a per-call arg).
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'nli',
        'kind', 'specialist',
        'specialist', 'nli_classify',
        'inputs', jsonb_build_object(
            'text', '{{ inputs.text }}',
            'candidate_labels', 'positive,negative,neutral,mixed'
        )
    )
)
WHERE name = 'sentiment';

-- contradicts(a, b) → nli_contradicts (returns YES/NO; yes_no parser
-- converts to bool).
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'nli',
        'kind', 'specialist',
        'specialist', 'nli_contradicts',
        'inputs', jsonb_build_object(
            'premise', '{{ inputs.a }}',
            'hypothesis', '{{ inputs.b }}'
        )
    )
)
WHERE name = 'contradicts';

-- supports(a, b) → nli_entails. Logically "A supports B" iff
-- A entails B (the model says the same thing).
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'nli',
        'kind', 'specialist',
        'specialist', 'nli_entails',
        'inputs', jsonb_build_object(
            'premise', '{{ inputs.a }}',
            'hypothesis', '{{ inputs.b }}'
        )
    )
)
WHERE name = 'supports';

-- implies(a, b) → nli_entails (same as supports under the standard
-- NLI definition). Kept as a separate operator for readability.
UPDATE rvbbit.operators
SET steps = jsonb_build_array(
    jsonb_build_object(
        'name', 'nli',
        'kind', 'specialist',
        'specialist', 'nli_entails',
        'inputs', jsonb_build_object(
            'premise', '{{ inputs.a }}',
            'hypothesis', '{{ inputs.b }}'
        )
    )
)
WHERE name = 'implies';

-- Bust the in-memory operator cache so the next call reloads the new
-- steps configuration immediately.
SELECT rvbbit.flush_cache();

-- Show what got wired.
SELECT name, return_type, parser,
       jsonb_path_query_first(steps, '$[*].specialist')#>>'{}' AS uses_specialist
FROM rvbbit.operators
WHERE name IN ('about', 'extract', 'classify', 'sentiment', 'contradicts', 'supports', 'implies')
ORDER BY name;
