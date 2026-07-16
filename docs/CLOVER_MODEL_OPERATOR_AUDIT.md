# Clover model/operator audit

Snapshot: 2026-07-16. Live target: `rabbit-zoo-core-template1` in
`us-east5-b`, reached by PostgreSQL through Hutch rather than by exposing the
zoo or vLLM ports publicly.

## Result

Every model resident in GPU memory has a managed Clover backend, one or more
SQL operators, and at least one successful metered invocation. The two useful
zoo routes that were implemented but not reachable through Hutch --
`/cluster` and `/tabular/explain` -- are now registered as `cluster` and
`tabular_explain` and are exercised by `clover_cluster` and `clover_explain`.

The managed package now advertises 49 public SQL operators: 28 specialist/ML
operators and 21 Gemma-backed operators. Forty-eight are registry operators
with 107 embedded test cases; `clover_llm_make_operator` is a guarded PL/pgSQL
authoring workflow. The final battery passes 106/107. The one known red case,
`clover_entails subset+`, is retained because the two-way zero-shot NLI model
does not reliably infer universal-to-existential entailment.

## Live resident inventory and utilization

The counts below are successful Hutch-metered requests observed from
2026-07-12 through the final v1.2 test run. They establish real route use; they
are not lifetime model counters. A request can contain multiple input rows.

| Resident model | Managed backend / route | SQL coverage | Successful use |
|---|---|---|---:|
| Snowflake Arctic Embed L v2.0 | `embed` / `/v1/embeddings`; `cluster` / `/cluster` | built-in embedding composites, `clover_similar`, `clover_cluster` | 1,892 embed; 8 cluster |
| BGE Reranker v2 M3 | `rerank` / `/rerank` | `clover_means`, `clover_relevance` | 818 |
| Twitter XLM-R sentiment | `sentiment` / `/sentiment` | `clover_sentiment`, `clover_sentiment_score` | 21,841 |
| DeBERTa v3 large zero-shot | `nli`, `classify` | `clover_entails`, `clover_classify`, `clover_classify_scores` | 356 NLI; 415 classify |
| DeBERTa v3 large 3-way NLI | `nli3` / `/nli?model=3class` | `clover_contradicts`, `clover_nli` | 167 |
| Toxic-BERT | `toxicity` / `/toxicity` | `clover_toxic`, `clover_moderate` | 5,231 |
| XLM-R language detection | `language` / `/language` | `clover_language`, `clover_language_info` | 704 |
| GLiNER large v2.1 | `extract` / `/extract` | `clover_extract`, `clover_pii` | 479 |
| SigLIP2 SO400M | `image_embed` / `/v1/image_embeddings` | `clover_image_similar`, `clover_image_embed` | 85 |
| Whisper large v3 turbo | `transcribe` / `/transcribe` | `clover_transcribe` | 19 |
| REBEL large | `relations` / `/relations` | `clover_relations` | 118 |
| TabPFN v2 classifier | `tabular_fit`, `tabular_predict`, `tabular_explain` | `clover_fit`, `clover_predict`, `clover_explain` | 49 fit; 59 predict; 14 explain |
| TabPFN v2 regressor | same routes as classifier | same operators, selected by task/model blob | loaded and startup-warmed; fit/predict meter is shared with classifier |
| Chronos-Bolt base | `forecast` / `/forecast` | `clover_forecast` | 85 |
| GOT-OCR2 | `ocr` / `/document/ocr` | `clover_ocr` | 21 |
| NVIDIA Gemma 4 31B IT NVFP4 (vLLM sibling) | `clover_llm` / OpenAI chat | 21 public LLM operators plus the internal drafting operator | 72,414 requests; 6,411,023 prompt and 268,758 generated tokens |

Isolation Forest is fitted per customer request rather than held as a resident
checkpoint. Its `anomaly_fit` and `anomaly_score` routes were already exposed;
they recorded 34 and 48 successful requests respectively. `clover_explain`
can also explain an anomaly-model blob with SHAP values.

## Operators added in v1.2

Specialist output that was previously stranded behind coarse wrappers is now
available directly:

- `clover_classify_scores(text, labels)` -- winner plus all candidate scores
- `clover_nli(premise, hypothesis)` -- full entailment/neutral/contradiction scores
- `clover_language_info(text)` -- language plus confidence
- `clover_image_embed(item)` -- reusable SigLIP2 vector
- `clover_cluster(values, num_clusters)` -- K-means or HDBSCAN over Arctic embeddings
- `clover_explain(model_blob_b64, model_sha256, features, feature_names)` -- SHAP attributions for TabPFN or anomaly models

The strongest reusable patterns recovered from the 205 legacy semantic-SQL
cascades became Gemma-backed operators:

- `clover_llm_same_entity(left_value, right_value, entity_type)`
- `clover_llm_merge_records(records, strategy)`
- `clover_llm_timeline(text, reference_date)`
- `clover_llm_consensus(texts, focus)`

Legacy summarize/condense/themes, outlier, topic, dedupe, and ungrounded
SQL-generation patterns were not duplicated because equivalent or safer
registry/composite operators already exist. `/classify_batch` also remains an
internal transport optimization: Hutch already batches SQL inputs, so a second
public SQL primitive would add no semantic capability.

## Deployment and contract checks

- Live zoo `/health/strict` and combined serving health returned HTTP 200 with
  all 15 gateway slots and Gemma ready.
- Live gateway code matched the audited local `main.py` and `metrics.py` hashes.
- Hutch was restarted without restarting the zoo or vLLM; its production
  configuration now has 20 specialist backends and the Gemma LLM entry.
- Authenticated end-to-end probes passed for clustering, TabPFN regression
  explanation, and anomaly-model explanation.
- The catalog installation executed all 120 SQL statements against PostgreSQL
  18 with pg_rvbbit 4.0.5.
- Persisted battery run 53 executed all 107 cases from the canonical catalog:
  106 passed, with only `clover_entails subset+` red.
- Backend provenance now records provider, exact model identity/revision label,
  and the managed install manifest for all 21 managed backend rows.
- Hutch, the SQL cost policy, and the catalog now agree on Gemma's included
  receipt valuation: $0.10/M input tokens and $0.20/M output tokens.

## Non-resident cache artifacts

The host cache also contains Qwen3 embedding 4B, BGE-M3, older sentiment
candidates, Donut, and an unused Gemma speculative-decoding checkpoint. None
is loaded, advertised, routed, or consuming VRAM. They are cache leftovers,
not unexposed hosted models; deleting them is a disk-hygiene decision rather
than an operator-coverage fix.
