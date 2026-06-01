# Bigfoot Field Notebook

This is a SQL-first mini-project for the RVBBIT docs. It loads the full
`bigfoot_sightings.csv` file with all columns, builds a cleaned reporting table,
then walks through semantic retrieval, classification, clustering, extraction,
knowledge graph context, and receipts.

No Python is used by the notebook. The loader uses `psql \copy` so the CSV can
be read from the client machine.

## Run

```bash
examples/bigfoot/run_all.sh
```

Defaults:

- `RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench`
- `BIGFOOT_SAMPLE_ROWS=500`
- `BIGFOOT_CLASSIFY_ROWS=250`
- `BIGFOOT_EXTRACT_ROWS=12`
- `BIGFOOT_KG_ROWS=250`

The live triples/receipt section calls the configured model provider. It is
opt-in:

```bash
BIGFOOT_LIVE=1 examples/bigfoot/run_all.sh
```

## Scripts

| Script | Purpose |
| --- | --- |
| `00_load.sql` | Load all CSV fields and create the cleaned notebook table. |
| `01_profile.sql` | Show dataset shape and field coverage. |
| `02_retrieval.sql` | Materialize embeddings, run KNN search, and show evidence snippets. |
| `03_semantic_map.sql` | Build semantic classifications, topics, outliers, diff, dedupe, extraction. |
| `04_knowledge_graph.sql` | Build a deterministic KG from report metadata and lexical clues. |
| `05_live_triples_receipts.sql` | Optional live model triples and receipt/cost inspection. |

Current note: `00_load.sql` uses `psql \copy` with the local path
`/home/ryanr/csv-files/bigfoot_sightings.csv`. If this demo needs to run on
another machine, update that path or run the loader from a wrapper that expands
the path before invoking `psql`.
