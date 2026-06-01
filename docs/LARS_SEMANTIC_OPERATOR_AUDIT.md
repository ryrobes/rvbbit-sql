# LARS Semantic Operator Audit for Warren Catalog Planning

Date: 2026-06-01

Source audited:
`/home/ryanr/repos2026/rabbit-lars/lars/lars/builtin_cascades/semantic_sql/*.yaml`

## Summary

The old LARS semantic SQL catalog has 205 cascade YAML files. The useful
migration shape is not "one Warren per old operator". Most of the old surface
area collapses into a small number of shared primitives:

| Primitive | Old files | Recommended packaging |
| --- | ---: | --- |
| General LLM operator pack | 66 | SQL/operator pack enabled by any configured LLM provider |
| LLM fallbacks and escape hatches | 44 | Same LLM pack; install as explicit `_llm` variants |
| Zero-shot/NLI classifier | 22 | Shared DeBERTa-style Warren creates many dimension/classifier operators |
| Embedding and semantic geometry | 15 | Shared embedding Warren plus SQL/Python operator pack |
| Rerank/relevance scorer | 3 | Shared cross-encoder Warren |
| Task classifiers | 5 | Small classifier Warrens for sentiment, toxicity, language, emotion |
| Extraction, media, time-series specialists | 7 | Specialist Warrens, probably shipped selectively |
| Deterministic SQL/Python | 20 | Native SQL where possible; CPython runtime for library-backed parsing/stats |
| External API/MCP/runtime integrations | 12 | MCP gateway or explicit external-service Warren packs |
| Visualization/reporting pipelines | 11 | Operator pack; use LLM/Python/render runtimes only where needed |

The strongest Warren starter-catalog story is therefore:

1. Ship model capabilities that are reusable across many operators.
2. Ship operator packs that bind those capabilities into ergonomic SQL.
3. Let a single installed Warren capability unlock multiple operators,
   dimensions, and sample flows.
4. Keep broad reasoning, synthesis, schema interpretation, and "escape hatch"
   behavior on the general LLM path.

## High-Level Read

The old semantic catalog already points toward the Warren design. The
specialist operators were not just cheaper versions of LLM calls. They were
ways to make semantic SQL scale across many rows without treating every row as
a chat completion. That dovetails with Warren well: a model capability is a
sidecar, and the SQL/operator layer is the higher-level product surface.

The starter catalog should avoid dozens of near-duplicate model packs. Instead,
the first useful catalog should have a compact set of reusable capabilities:

| Starter pack | Provides | Operators/flows it unlocks |
| --- | --- | --- |
| `runtimes/mcp-gateway` | SQL MCP calls and `kind: mcp` pipeline nodes | Web tools, external APIs, tool-backed flows |
| `runtimes/python-runtime` | SQL-managed CPython envs and `kind: python` nodes | Deterministic transforms, validators, stats, custom glue |
| `classify/deberta-v3-base-zero-shot` | Closed-label zero-shot classification | `bucket`, `category`, `intent`, `stance`, `looks_like`, `semantic_case`, `semantic_switch`, etc. |
| `classify/deberta-v3-zero-shot` | Larger/higher-quality zero-shot classification | Same surface as above, for GPU/higher quality installs |
| `embeddings/bge-m3` | Multilingual long-context embeddings | `embed`, `similar_to`, `dedupe`, `cluster`, `topics`, `themes`, `semantic_match`, vector search |
| `embeddings/bge-small-en-v1.5` or `embeddings/e5-small-v2` | CPU-friendly embeddings | Same embedding family when cheap local CPU is preferred |
| `rerank/bge-reranker-v2-m3` | Query/document relevance scoring | `matches`, `score`, criteria-based `outliers`, ranking/filtering flows |
| `rerank/ms-marco-minilm-l6-v2` | CPU-friendly reranking | Same rerank family for small installs |
| `classify/twitter-roberta-sentiment` | English sentiment classification | `sentiment`, review/ticket analytics flows |
| `classify/toxic-bert` | Toxicity/moderation classification | `toxicity`, moderation/data-quality flows |
| `classify/language-detection-xlm-roberta` | Language detection | `language`, multilingual routing flows |
| `classify/emotion-distilroberta` | Emotion classification | New/adjacent to old `vibes`/sentiment workflows |
| `extract/gliner-medium-v2.1` | Zero-shot entity extraction | `extract_entities`, `extract_pii`, `extract_business_entities`; partial replacement for some `extracts` use cases |

## General LLM Operators

These should not require a model Warren by default. They should be installed
when an LLM provider is configured, or as an operator pack that depends on the
existing router/provider catalog.

Use the general LLM path for:

- Open-ended reasoning: `counterargument`, `steelman`, `weaknesses`,
  `fallacy`, `assumes`, `evidence_type`.
- Natural-language SQL and data interaction: `ask`, `ask_data`,
  `ask_data_sql`, `sql_expression`.
- Schema-following extraction: `extract_structured`, `smart_json`,
  `triples`, `timeline`, `timeline_agg`.
- Messy data repair when deterministic parsing is not enough: `fix`,
  `fill`, `complete`, `canonical`, `cast_smart`, `valid`, `validate`.
- Pipeline operations that are really instructions over a result set:
  `ANALYZE`, `FILTER`, `ENRICH`, `GROUP`, `PIVOT`, `MELT`, `TOP`.
- Explicit escape hatches: every `*_llm.cascade.yaml` file.

Recommendation: create a `operators/semantic-llm-core` catalog item. It does
not need to deploy Docker. It should install SQL operator definitions, examples,
and acceptance flows, and it should show "requires LLM provider" in the UI.

Open design item: current capability manifests only allow `hf_backend` and
`runtime_sidecar`. To model this cleanly, add an `operator_pack` or
`operator_bundle` manifest kind that can declare dependencies on runtime or
backend capabilities without launching a new container.

## Specialist Warren Candidates

### Zero-Shot/NLI Classification

Old files: 22.

Good Warren shape: one zero-shot classifier sidecar, many SQL operators. The
operator pack supplies label sets and prompt templates; the Warren supplies the
model.

Best starter models:

- `MoritzLaurer/deberta-v3-base-zeroshot-v2.0`: CPU-friendlier baseline,
  already represented by `classify/deberta-v3-base-zero-shot`.
- `MoritzLaurer/DeBERTa-v3-large-mnli-fever-anli-ling-wanli`: higher quality,
  already represented by `classify/deberta-v3-zero-shot`.
- Commercially stricter variant to consider later: `*-c` models from the same
  DeBERTa zero-shot series.

Useful old operators unlocked:

- `audience`, `authenticity`, `bucket`, `category`, `complexity`,
  `credibility`, `domain`, `engagement`, `formality`, `intent`, `narrative`,
  `stance`, `timeframe`, `virality`
- `classify_single`, `looks_like`, `semantic_case`, `semantic_switch`
- `contradicts`, `implies`
- `extracts` for simple closed-label fact/entity extraction

Notes:

- `supports` is tagged as NLI in the old list, but the inspected cascade uses
  an LLM numeric rubric. It can remain LLM-first until we prove an NLI or
  reranker scoring version is accurate enough.
- NLI works best when the answer space is closed. If the operator invents new
  labels, summaries, explanations, or schemas, keep it LLM-first.

Sources:

- https://huggingface.co/MoritzLaurer/deberta-v3-base-zeroshot-v2.0

### Embeddings and Semantic Geometry

Old files: 15.

Good Warren shape: one embedding backend plus an operator pack that implements
collection-level algorithms in SQL/Python. This gives a lot of leverage because
embedding vectors are the shared primitive for dedupe, semantic joins, topic
grouping, centrality, clustering, and vector search.

Best starter models:

- `BAAI/bge-m3`: best default for multilingual, longer text, and scale.
- `BAAI/bge-small-en-v1.5`: CPU-friendly English option.
- `intfloat/e5-small-v2`: CPU-friendly retrieval option.

Useful old operators unlocked:

- Direct embedding: `embed`, `embed_with_storage`, `embed_column`,
  `embed_status`
- Similarity/search: `similar_to`, `semantic_match`, `match_pair`,
  `vector_search`
- Collection transforms: `dedupe`, `cluster`, `topics`, `themes`,
  `consensus`, `vibes`
- Collection classification: `classify`

Implementation note:

- Keep the sidecar narrow: `/predict` returns embeddings.
- Put algorithms such as k-means, centroid centrality, threshold graphs,
  component picking, and topic assignment in the operator pack or CPython
  runtime. That keeps model packs reusable and inspectable.

Sources:

- https://huggingface.co/BAAI/bge-m3
- https://arxiv.org/abs/2402.03216

### Rerank/Relevance

Old files: 3.

Good Warren shape: one cross-encoder relevance backend plus SQL operators for
boolean filters, scores, row ranking, and criteria-guided outlier detection.

Best starter models:

- `BAAI/bge-reranker-v2-m3`: multilingual default, already in catalog.
- `BAAI/bge-reranker-base`: smaller BGE option.
- `cross-encoder/ms-marco-MiniLM-L6-v2`: CPU-friendly English option, already
  in catalog.

Useful old operators unlocked:

- `matches`
- `score`
- `outliers` when criteria is supplied

Sources:

- https://huggingface.co/BAAI/bge-reranker-v2-m3

### Task Classifiers

Old files: 5, plus one obvious new catalog addition already present for
emotion.

Good Warren shape: separate small model packs for high-volume row classifiers.
These are cheap, fast, and easy for users to understand in a catalog.

Best starter models:

- Sentiment: `cardiffnlp/twitter-roberta-base-sentiment-latest`
- Toxicity: `unitary/toxic-bert`
- Language: `papluca/xlm-roberta-base-language-detection`
- Emotion: `j-hartmann/emotion-english-distilroberta-base`

Useful old operators unlocked:

- `sentiment`, `sentiment_scalar`, `sentiment_dimension`
- `toxicity`
- `language`
- Adjacent/new: emotion classification for support, survey, and conversation
  analytics.

Notes:

- `sentiment(focus => ...)` in the old cascade falls back to zero-shot labels
  like "High excitement". That can reuse the zero-shot Warren rather than the
  sentiment-specific model.
- Toxicity classifiers require careful UI copy and docs because bias and false
  positives are material operational risks.

Sources:

- https://huggingface.co/cardiffnlp/twitter-roberta-base-sentiment-latest
- https://huggingface.co/unitary/toxic-bert
- https://huggingface.co/papluca/xlm-roberta-base-language-detection

### Extraction, Media, and Time-Series Specialists

Old files: 7.

These are good candidates, but not all should be first-day catalog items.

| Operator family | Old files | Candidate models/packs | Recommendation |
| --- | --- | --- | --- |
| Entity extraction | `extracts`, plus new entity operators | `urchade/gliner_medium-v2.1` | Already a good starter Warren; use for NER/PII/business entities, not full relation extraction. |
| Relation extraction | `relations` | REBEL-style relation extraction model, or LLM-first | Defer unless we add a custom relation handler. GLiNER extracts entities, not predicates. |
| Document parsing/OCR | `parse_document` | Qwen2.5-VL class models, Docling-style OCR/table parsers | Worth a future pack, but operationally heavier than text classifiers. |
| Speech-to-text | `transcribe` | Whisper small/base variants | Good future specialist Warren; clear value and separate compute profile. |
| Image/text similarity | `image_embed`, `image_matches`, `image_similarity` | CLIP, SigLIP, OpenCLIP | Good future Warren if image columns are in scope. |
| Time-series forecasting | `forecast` | `amazon/chronos-bolt-base` or smaller Chronos-Bolt variants | Strong Warren candidate; old operator already assumes Chronos. |

Sources:

- https://arxiv.org/abs/2311.08526
- https://huggingface.co/openai/clip-vit-base-patch32
- https://huggingface.co/openai/whisper-small.en
- https://huggingface.co/amazon/chronos-bolt-base
- https://arxiv.org/abs/2502.13923

## Deterministic SQL/Python Operators

Old files: 20.

These should not be LLM operators unless the user explicitly asks for fuzzy
repair. They are better as native SQL, Rust, or CPython-runtime utilities:

- Stats/privacy: `ab_test`, `dp_count`, `dp_mean`, `kaplan_meier`
- Graph: `pagerank`, `shortest_path`
- Parsing/normalization: `parse_email`, `parse_date`, `parse_name`,
  `parse_phone`, `parse_address`, `normalize_currency`, `normalize_quantity`,
  `normalize_unit`, `clean_year`
- Workflow state/helpers: `param_set`, `param_get`, `param_clear`, `latest`
- Matching: `sounds_like`

Recommendation: create a `operators/data-quality-python` pack that depends on
`runtimes/python-runtime`, seeds one or more SQL-managed Python envs, and
installs deterministic handler/operator examples. This is where the new
`kind: python` primitive becomes a force multiplier without pretending every
data cleaning task is an ML model.

## External API and MCP Operators

Old files: 12.

These should be MCP gateway or external-service capabilities, not model
sidecars:

- Web/search/crawl: `web_search_fc`, `web_scrape`, `web_extract`, `web_map`,
  `web_agent`, `scrape`, `crawl_batch`, `refresh_crawl`, `summarize_urls`
- Feeds: `rss`
- External vector DB: `embed_batch_pinecone`, `vector_search_pinecone`

Recommendation: create explicit capability packs for external service families,
starting with a Firecrawl/MCP-style web pack. The pack should install:

- MCP server registration or endpoint configuration.
- SQL wrappers/operators.
- Acceptance tests that use a tiny stable fixture page or mocked/test endpoint
  when possible.
- Clear credential requirements in the manifest metadata.

This is exactly the kind of thing that benefits from Warren plus MCP gateway:
the database sees a SQL operator, while the operational surface stays outside
the Postgres backend process.

## Visualization and Reporting Pipelines

Old files: 11.

Most visualization operators are not model packs. They are operator packs that
compose LLM planning, deterministic rendering, and possibly Python/runtime
tools:

- Chart/spec generation: `to_plotly`, `to_vegalite`
- Graph/timeline render helpers: `mermaid_timeline`, `mermaid_triples`,
  `to_property_graph`, `rich_triples`
- Layout/rendering: `add_styles`, `render_canvas`, `render`, `stylize`
- Profiling/reporting: `stats`

Recommendation: keep these as a later `operators/reporting-and-viz` pack. It
should depend on:

- LLM provider for chart/spec generation and narrative summaries.
- CPython runtime for deterministic rendering or table shaping.
- Optional media/image specialist only for `stylize`.

## Catalog Design Implications

The current catalog is model-first, which is good for Warren deployment. The
old semantic operators show that we also need a clean "operator bundle" layer:

- A model capability deploys one backend or runtime.
- An operator bundle installs SQL functions/operators, flows, validators, and
  examples.
- An operator bundle can require one or more capabilities, such as
  `embedding`, `rerank`, `python-runtime`, or `mcp-gateway`.
- A catalog entry should show both "what gets deployed" and "what SQL surface
  becomes available".

Minimal schema additions to consider:

```yaml
kind: operator_pack
requires:
  capabilities:
    - embeddings/bge-m3
  roles:
    - embedding
exports:
  operators:
    - semantic_embed
    - semantic_dedupe
    - semantic_match
  flows:
    - customer_feedback_topic_cluster
acceptance:
  tests:
    - name: semantic_dedupe_names
      sql: ...
```

This would let the UI say: "Installing BGE-M3 unlocks these 12 operators and 3
example flows" without forcing every operator into the model pack itself.

## Proposed Built-In Catalog Roadmap

### Phase 1: Core Text Analytics

Ship or keep:

- `classify/deberta-v3-base-zero-shot`
- `classify/deberta-v3-zero-shot`
- `embeddings/bge-m3`
- `embeddings/bge-small-en-v1.5`
- `embeddings/e5-small-v2`
- `rerank/bge-reranker-v2-m3`
- `rerank/ms-marco-minilm-l6-v2`
- `classify/twitter-roberta-sentiment`
- `classify/toxic-bert`
- `classify/language-detection-xlm-roberta`
- `classify/emotion-distilroberta`
- `extract/gliner-medium-v2.1`
- `runtimes/python-runtime`
- `runtimes/mcp-gateway`

Add:

- `operators/semantic-llm-core`
- `operators/semantic-zero-shot-dimensions`
- `operators/semantic-embedding-workflows`
- `operators/semantic-rerank-workflows`
- `operators/data-quality-python`

### Phase 2: External Data and Web

Add:

- `external/firecrawl-web`
- `external/rss-reader`
- `external/pinecone-vector-search` only if Pinecone remains a priority.

These should depend on `runtimes/mcp-gateway` unless there is a strong reason
to ship a dedicated service.

### Phase 3: Media and Forecasting

Add selectively:

- `timeseries/chronos-bolt`
- `speech/whisper-small`
- `vision/clip-or-siglip`
- `documents/qwen-vl-or-docling`
- `extract/relation-extraction`

These are valuable but heavier. They should not be required for the starter
experience.

## Appendix A: Per-File Audit

Every audited YAML file is accounted for below. The grouping is the migration
recommendation, not necessarily the old tag value.

### General LLM or LLM-Managed Operator Pack (66)

`aligns.cascade.yaml`, `analyze_pipeline.cascade.yaml`,
`anonymize_single.cascade.yaml`, `ask.cascade.yaml`,
`ask_data.cascade.yaml`, `ask_data_sql.cascade.yaml`,
`assess_confidence.cascade.yaml`, `assumes.cascade.yaml`,
`best_agg.cascade.yaml`, `canonical_single.cascade.yaml`,
`cast_smart_single.cascade.yaml`, `coalesce_smart_agg.cascade.yaml`,
`compare_agg.cascade.yaml`, `complete_single.cascade.yaml`,
`condense.cascade.yaml`, `correct_single.cascade.yaml`,
`counterargument.cascade.yaml`, `dedupe_pipeline.cascade.yaml`,
`default_smart_single.cascade.yaml`, `enrich_pipeline.cascade.yaml`,
`evidence_type.cascade.yaml`, `extract_structured.cascade.yaml`,
`fallacy.cascade.yaml`, `fill_single.cascade.yaml`,
`filter_pipeline.cascade.yaml`, `fix_single.cascade.yaml`,
`formalize_single.cascade.yaml`, `generic_discriminator.cascade.yaml`,
`golden_record_agg.cascade.yaml`, `group_pipeline.cascade.yaml`,
`impute_single.cascade.yaml`, `infer_type_single.cascade.yaml`,
`investigate_pipeline.cascade.yaml`, `match_template.cascade.yaml`,
`melt_pipeline.cascade.yaml`, `merge_records_agg.cascade.yaml`,
`merge_texts_agg.cascade.yaml`, `normalize_single.cascade.yaml`,
`parse.cascade.yaml`, `parse_single.cascade.yaml`,
`parse_value.cascade.yaml`, `pass_pipeline.cascade.yaml`,
`pivot_pipeline.cascade.yaml`, `python_pipeline.cascade.yaml`,
`quality_single.cascade.yaml`, `rank_agg.cascade.yaml`,
`same_as_single.cascade.yaml`, `sample_pipeline.cascade.yaml`,
`skill.cascade.yaml`, `skill_json.cascade.yaml`,
`smart_json.cascade.yaml`, `smart_split_single.cascade.yaml`,
`smart_translate_single.cascade.yaml`, `smart_unpack_single.cascade.yaml`,
`speak_pipeline.cascade.yaml`, `sql_expression.cascade.yaml`,
`steelman.cascade.yaml`, `summarize.cascade.yaml`,
`timeline.cascade.yaml`, `timeline_agg.cascade.yaml`,
`top_pipeline.cascade.yaml`, `triples.cascade.yaml`,
`unnest_smart_single.cascade.yaml`, `valid_single.cascade.yaml`,
`validate_single.cascade.yaml`, `weaknesses.cascade.yaml`.

### LLM Fallbacks and Escape Hatches (44)

`audience_dimension_llm.cascade.yaml`,
`authenticity_dimension_llm.cascade.yaml`,
`bucket_dimension_llm.cascade.yaml`,
`category_dimension_llm.cascade.yaml`, `classify_llm.cascade.yaml`,
`classify_single_llm.cascade.yaml`, `clean_year_llm.cascade.yaml`,
`complexity_dimension_llm.cascade.yaml`, `consensus_llm.cascade.yaml`,
`contradicts_llm.cascade.yaml`, `credibility_dimension_llm.cascade.yaml`,
`dedupe_llm.cascade.yaml`, `domain_dimension_llm.cascade.yaml`,
`engagement_dimension_llm.cascade.yaml`, `extracts_llm.cascade.yaml`,
`formality_dimension_llm.cascade.yaml`, `implies_llm.cascade.yaml`,
`intent_dimension_llm.cascade.yaml`, `language_dimension_llm.cascade.yaml`,
`looks_like_single_llm.cascade.yaml`, `matches_llm.cascade.yaml`,
`narrative_dimension_llm.cascade.yaml`,
`normalize_currency_single_llm.cascade.yaml`,
`normalize_quantity_single_llm.cascade.yaml`,
`normalize_unit_single_llm.cascade.yaml`, `outliers_llm.cascade.yaml`,
`parse_address_single_llm.cascade.yaml`,
`parse_date_single_llm.cascade.yaml`, `parse_email_llm.cascade.yaml`,
`parse_name_single_llm.cascade.yaml`, `parse_phone_single_llm.cascade.yaml`,
`quality_single_llm.cascade.yaml`, `score_llm.cascade.yaml`,
`semantic_case_llm.cascade.yaml`, `semantic_switch_llm.cascade.yaml`,
`sentiment_dimension_llm.cascade.yaml`, `sounds_like_llm.cascade.yaml`,
`stance_dimension_llm.cascade.yaml`, `themes_llm.cascade.yaml`,
`timeframe_dimension_llm.cascade.yaml`, `topics_dimension_llm.cascade.yaml`,
`toxicity_dimension_llm.cascade.yaml`, `vibes_llm.cascade.yaml`,
`virality_dimension_llm.cascade.yaml`.

### Zero-Shot/NLI Specialist Warren (22)

`audience_dimension.cascade.yaml`, `authenticity_dimension.cascade.yaml`,
`bucket_dimension.cascade.yaml`, `category_dimension.cascade.yaml`,
`classify_single.cascade.yaml`, `complexity_dimension.cascade.yaml`,
`contradicts.cascade.yaml`, `credibility_dimension.cascade.yaml`,
`domain_dimension.cascade.yaml`, `engagement_dimension.cascade.yaml`,
`extracts.cascade.yaml`, `formality_dimension.cascade.yaml`,
`implies.cascade.yaml`, `intent_dimension.cascade.yaml`,
`looks_like_single.cascade.yaml`, `narrative_dimension.cascade.yaml`,
`semantic_case.cascade.yaml`, `semantic_switch.cascade.yaml`,
`stance_dimension.cascade.yaml`, `supports.cascade.yaml`,
`timeframe_dimension.cascade.yaml`, `virality_dimension.cascade.yaml`.

### Embedding/Similarity Specialist Warren (15)

`classify.cascade.yaml`, `cluster.cascade.yaml`, `consensus.cascade.yaml`,
`dedupe.cascade.yaml`, `embed.cascade.yaml`, `embed_column.cascade.yaml`,
`embed_status.cascade.yaml`, `embed_with_storage.cascade.yaml`,
`match_pair.cascade.yaml`, `semantic_match.cascade.yaml`,
`similar_to.cascade.yaml`, `themes.cascade.yaml`,
`topics_dimension.cascade.yaml`, `vector_search.cascade.yaml`,
`vibes.cascade.yaml`.

### Rerank/Relevance Specialist Warren (3)

`matches.cascade.yaml`, `outliers.cascade.yaml`, `score.cascade.yaml`.

### Task Classifier Specialist Warren (5)

`language_dimension.cascade.yaml`, `sentiment.cascade.yaml`,
`sentiment_dimension.cascade.yaml`, `sentiment_scalar.cascade.yaml`,
`toxicity_dimension.cascade.yaml`.

### Extraction, Media, and Time-Series Specialist Warren (7)

`relations.cascade.yaml`, `parse_document.cascade.yaml`,
`transcribe.cascade.yaml`, `forecast.cascade.yaml`,
`image_embed.cascade.yaml`, `image_matches.cascade.yaml`,
`image_similarity.cascade.yaml`.

### Deterministic SQL/Python Runtime (20)

`ab_test.cascade.yaml`, `clean_year.cascade.yaml`, `dp_count.cascade.yaml`,
`dp_mean.cascade.yaml`, `kaplan_meier.cascade.yaml`, `latest.cascade.yaml`,
`normalize_currency_single.cascade.yaml`,
`normalize_quantity_single.cascade.yaml`, `normalize_unit_single.cascade.yaml`,
`pagerank.cascade.yaml`, `param_clear.cascade.yaml`, `param_get.cascade.yaml`,
`param_set.cascade.yaml`, `parse_address_single.cascade.yaml`,
`parse_date_single.cascade.yaml`, `parse_email.cascade.yaml`,
`parse_name_single.cascade.yaml`, `parse_phone_single.cascade.yaml`,
`shortest_path.cascade.yaml`, `sounds_like.cascade.yaml`.

### External API/MCP/Runtime Integration (12)

`crawl_batch.cascade.yaml`, `embed_batch_pinecone.cascade.yaml`,
`refresh_crawl.cascade.yaml`, `rss.cascade.yaml`, `scrape.cascade.yaml`,
`summarize_urls.cascade.yaml`, `vector_search_pinecone.cascade.yaml`,
`web_agent.cascade.yaml`, `web_extract.cascade.yaml`, `web_map.cascade.yaml`,
`web_scrape.cascade.yaml`, `web_search_fc.cascade.yaml`.

### Visualization/Reporting Pipeline Pack (11)

`add_styles_pipeline.cascade.yaml`, `mermaid_timeline_pipeline.cascade.yaml`,
`mermaid_triples_pipeline.cascade.yaml`,
`render_canvas_pipeline.cascade.yaml`, `render_pipeline.cascade.yaml`,
`rich_triples.cascade.yaml`, `stats_pipeline.cascade.yaml`,
`stylize_pipeline.cascade.yaml`, `to_plotly_pipeline.cascade.yaml`,
`to_property_graph.cascade.yaml`, `to_vegalite_pipeline.cascade.yaml`.

