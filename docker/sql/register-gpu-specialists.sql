-- Register the GPU sidecar services as rvbbit specialists. Idempotent —
-- safe to re-run. Run from the host with:
--
--   make register-specialists
--
-- or manually:
--
--   docker compose -f docker/docker-compose.yml \
--                  -f docker/docker-compose.sidecars.yml \
--                  exec pg-rvbbit psql -U postgres -d bench \
--                  -f /docker/sql/register-gpu-specialists.sql

-- embed: BAAI/bge-m3 (1024-dim multilingual), native rvbbit transport.
SELECT rvbbit.register_backend(
    backend_name      => 'embed',
    backend_endpoint  => 'http://embed:8080/predict',
    backend_transport => 'rvbbit',
    backend_batch_size => 64,
    backend_max_concur => 4,
    backend_timeout_ms => 120000,
    backend_opts      => jsonb_build_object('model', 'BAAI/bge-m3'),
    backend_description => 'Sentence embeddings (bge-m3 GPU). Default for rvbbit.embed / similarity / knn_text / topics / outliers / dedupe_groups / diff / semantic_case.'
);

-- rerank: BAAI/bge-reranker-v2-m3 cross-encoder, GRADIO transport.
SELECT rvbbit.register_backend(
    backend_name      => 'rerank',
    backend_endpoint  => 'http://rerank:7860/api/predict',
    backend_transport => 'gradio',
    backend_batch_size => 1,
    backend_max_concur => 8,
    backend_timeout_ms => 60000,
    backend_opts      => jsonb_build_object('model', 'BAAI/bge-reranker-v2-m3'),
    backend_description => 'Cross-encoder relevance scoring (bge-reranker-v2-m3 GPU via Gradio). Use in rvbbit.about / score operators.'
);

-- extract: GLiNER medium, native rvbbit transport.
SELECT rvbbit.register_backend(
    backend_name      => 'extract',
    backend_endpoint  => 'http://extract:8080/predict',
    backend_transport => 'rvbbit',
    backend_batch_size => 16,
    backend_max_concur => 4,
    backend_timeout_ms => 60000,
    backend_opts      => jsonb_build_object('model', 'urchade/gliner_medium-v2.1'),
    backend_description => 'Zero-shot NER over arbitrary descriptive labels (GLiNER GPU). Powers rvbbit.extract.'
);

-- nli_classify: zero-shot classification via deberta-v3-large.
-- One container behind THREE specialist registrations; each points at
-- a different path so the operator's `steps` selects mode by URL.
SELECT rvbbit.register_backend(
    backend_name      => 'nli_classify',
    backend_endpoint  => 'http://nli:8080/classify',
    backend_transport => 'rvbbit',
    backend_batch_size => 32,
    backend_max_concur => 4,
    backend_timeout_ms => 60000,
    backend_opts      => jsonb_build_object('model', 'MoritzLaurer/deberta-v3-large-zeroshot-v2.0'),
    backend_description => 'Zero-shot label argmax (deberta-v3-large). Powers rvbbit.sentiment / rvbbit.classify.'
);

-- nli_entails: P(premise entails hypothesis) thresholded to YES/NO.
SELECT rvbbit.register_backend(
    backend_name      => 'nli_entails',
    backend_endpoint  => 'http://nli:8080/entails',
    backend_transport => 'rvbbit',
    backend_batch_size => 64,
    backend_max_concur => 4,
    backend_timeout_ms => 60000,
    backend_opts      => jsonb_build_object('model', 'MoritzLaurer/deberta-v3-large-zeroshot-v2.0'),
    backend_description => 'Raw NLI entailment (deberta-v3-large). Powers rvbbit.supports / rvbbit.implies.'
);

-- nli_contradicts: P(premise contradicts hypothesis) thresholded to YES/NO.
SELECT rvbbit.register_backend(
    backend_name      => 'nli_contradicts',
    backend_endpoint  => 'http://nli:8080/contradicts',
    backend_transport => 'rvbbit',
    backend_batch_size => 64,
    backend_max_concur => 4,
    backend_timeout_ms => 60000,
    backend_opts      => jsonb_build_object('model', 'MoritzLaurer/deberta-v3-large-zeroshot-v2.0'),
    backend_description => 'Raw NLI contradiction (deberta-v3-large). Powers rvbbit.contradicts.'
);

-- Refresh the in-process spec cache so the next call sees these.
SELECT rvbbit.reload_backends();

-- Show what's registered.
SELECT name, transport, endpoint_url, batch_size, max_concurrent,
       transport_opts->>'model' AS model
FROM rvbbit.backends
WHERE name IN ('embed', 'rerank', 'extract', 'nli_classify', 'nli_entails', 'nli_contradicts')
ORDER BY name;

-- The companion wire-operators-to-specialists.sql is applied
-- automatically by `make register-specialists` after this file runs.
