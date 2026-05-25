-- Restore the default local CPU embedding backend.
--
-- GPU demos register `embed` to point at the BGE-M3 sidecar so existing SQL
-- keeps using rvbbit.embed / rvbbit.knn_text without an extra backend name.
-- Run this file when you want the fresh-install default again.

SELECT rvbbit.register_backend(
    backend_name        => 'embed',
    backend_endpoint    => 'local://embed',
    backend_transport   => 'local_embed',
    backend_batch_size  => 128,
    backend_max_concur  => 1,
    backend_timeout_ms  => 120000,
    backend_opts        => '{"model":"bge-small-en-v1.5"}'::jsonb,
    backend_description => 'Default local CPU text embedding backend.'
);

SELECT rvbbit.reload_backends();

SELECT name, transport, endpoint_url, batch_size, max_concurrent,
       transport_opts->>'model' AS model
FROM rvbbit.backends
WHERE name = 'embed';
