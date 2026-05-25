-- pg_rvbbit 0.38.0 -> 0.39.0
-- Local CPU embedding backend. The default `embed` backend is only seeded
-- when the user has not already registered one.

ALTER TABLE rvbbit.backends
    DROP CONSTRAINT IF EXISTS backends_transport_check;

ALTER TABLE rvbbit.backends
    ADD CONSTRAINT backends_transport_check
        CHECK (transport IN ('rvbbit', 'gradio', 'openai', 'local_embed', 'stub',
                             'openai_chat', 'anthropic', 'gemini'));

INSERT INTO rvbbit.backends
    (name, transport, endpoint_url, batch_size, max_concurrent, timeout_ms,
     transport_opts, description)
VALUES
    ('embed', 'local_embed', 'local://embed', 128, 1, 120000,
     '{"model":"bge-small-en-v1.5"}'::jsonb,
     'Default local CPU text embedding backend.')
ON CONFLICT (name) DO NOTHING;
