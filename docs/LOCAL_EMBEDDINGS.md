# Local Embeddings

For the broader embedding usage guide, see [Rvbbit Embeddings](EMBEDDINGS.md).

Rvbbit ships a local CPU embedding transport named `local_embed`. It runs
text embedding models in-process through `fastembed`/ONNX Runtime, so the
standard embedding functions work without a sidecar or external API key.

Fresh installs seed this backend:

```sql
SELECT name, transport, endpoint_url, batch_size, transport_opts
FROM rvbbit.backends
WHERE name = 'embed';
```

Expected default:

```text
name      = embed
transport = local_embed
endpoint  = local://embed
opts      = {"model":"bge-small-en-v1.5"}
```

The `embed` backend is not hardcoded. It is only a row in
`rvbbit.backends`, and users can replace it at any time:

```sql
SELECT rvbbit.register_backend(
    backend_name      => 'embed',
    backend_endpoint  => 'https://api.openai.com/v1/embeddings',
    backend_transport => 'openai',
    backend_auth_env  => 'OPENAI_API_KEY',
    backend_opts      => '{"model":"text-embedding-3-small"}'::jsonb
);
SELECT rvbbit.reload_backends();
```

Then existing SQL keeps using the new provider:

```sql
SELECT rvbbit.embed('hello');
SELECT * FROM rvbbit.knn_text('docs'::regclass, 'body', 'refund request', 10);
```

## Options

`local_embed` reads these `transport_opts`:

| option | meaning |
|---|---|
| `model` | FastEmbed model name or common alias. Default: `bge-small-en-v1.5`. |
| `cache_dir` | Model cache directory. Defaults to FastEmbed's normal cache path. |
| `max_length` | Token length cap, clamped to `8..8192`. |

Useful aliases include:

- `bge-small-en-v1.5`
- `bge-small-en-v1.5-q`
- `all-MiniLM-L6-v2`
- `all-MiniLM-L6-v2-q`
- `bge-m3`
- `nomic-embed-text-v1.5`

Environment fallbacks:

- `RVBBIT_LOCAL_EMBED_MODEL`
- `RVBBIT_LOCAL_EMBED_CACHE`

The first call may download model files into the cache directory unless the
Docker image or host has pre-populated that cache.
