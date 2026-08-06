# Brain query-source providers (MCP / SQL-backed documents)

A brain **query source** indexes anything a SQL query can yield as if it were a document folder: MCP
artifacts (Linear issues, Fireflies meetings, GitHub PRs…), other tables, computed views. A **provider**
is the reusable recipe; a **source** binds it. A source update fetches and reconciles content, ACLs,
chunks, and embeddings first, making documents searchable. The durable Brain worker then processes
optional derived briefs plus KG/NER edges one document and one commit at a time. A slow or failed
semantic call cannot roll back already-searchable documents or earlier completed knowledge work.

`rvbbit.mcp_rows(server, tool, args jsonb)` (SETOF jsonb, one row per item) is the bridge to any
registered MCP server. The provider's `list_sql` just has to project the canonical columns.

## The contract — what `list_sql` must return

| column         | type          | required | notes |
|----------------|---------------|----------|-------|
| `uri`          | text          | ✅       | stable external id, namespaced (`linear:<id>`, `fireflies:<id>`). Drives dedup. |
| `title`        | text          |          | display title |
| `content_hash` | text          |          | change token. Same hash ⇒ skipped (no re-embed). Absent ⇒ always re-ingest. Use a version field (`updatedAt`) or `md5()` of the meaningful fields. |
| `occurred_at`  | timestamptz   |          | event time (powers time filters + recency) |
| `body`         | text          | ✅*      | the text that gets chunked + embedded (*single-phase: here; two-phase: from `item_sql`) |
| `props`        | jsonb         |          | the raw artifact JSON — read by the **edge_map** for structured KG edges |

The engine wraps `list_sql` as `SELECT to_jsonb(q) FROM (<list_sql>) q`, so **any extra columns are
ignored** and missing ones are NULL — the projection is forgiving. `$1` is bound to the source's
`config` jsonb if the inner SQL references it (per-source params).

**Two-phase** (list returns ids → fetch each): set `item_sql` with `$1` = uri, returning
`(body, title, occurred_at[, props])`; only NEW/CHANGED uris are fetched. Single-phase = `item_sql` NULL,
`body` comes from `list_sql`.

## Defining a provider

```sql
rvbbit.brain_define_provider(
  provider, label, list_sql,
  item_sql   default null,        -- null = single-phase
  icon       default null,
  description default null,
  edge_map   default '[]',        -- [{predicate, kind, path}] — path is a JSONPath into props
  doc_type   default 'document')  -- the type every doc from this provider is tagged with
```

`edge_map` asserts deterministic KG edges (no LLM): for each spec, `jsonb_path_query(props, path)` →
a `document --predicate--> kind:value` edge **plus** a `mentions` edge (so the entity drives
`brain_related` overlap). `doc_type` is the facet agents/UI filter on — keep it low-cardinality, custom is
fine (`document | ticket | meeting | pr | …`); see `brain_facets(email)` and `ask_brain(..., filter)`.

Then: `brain_add_query_source(label, provider)` → `brain_update_source(source_id)` (or the automatic
source-update job / Lens **Update** button). `CALL brain_enrich_drain()` continuously drains every
outstanding knowledge task; Lens calls that **Process now** when an operator wants to start it early.
Query-source docs are **global** (visible to any authenticated caller); ACL is the `is_public`
synthetic role.

## mcp_rows gotchas

- **`format`**: many MCP tools default to a token-efficient non-JSON format ("toon"). Pass
  `"format":"json"` so `mcp_rows` can parse fields. (Fireflies defaults to toon.)
- **Unwrapping**: `mcp_rows` returns one row per element if the response is a top-level array or an
  object with a known array key (items/results/data/entries/rows); otherwise the whole object is one row.
  Probe the shape (`jsonb_each`, `jsonb_object_keys`) before mapping.
- **Caps + pagination**: tools often cap result size. Follow an offset/cursor to exhaustion when one is
  available; otherwise fan out by a stable filter dimension. Never interpret a partial page as an
  authoritative deletion list.

---

## Worked example: Linear issues (`linear-issues`, two providers, by-project fan-out)

`linear_getIssues` only returns the most-recent N (no cursor, crashes at high limits), so fan out across
projects. `getProjectIssues` omits `project` (you queried by it) → inject it from the outer row.

```sql
SELECT 'linear:'||(r->>'id') AS uri,
       concat_ws(' · ', r->>'identifier', r->>'title') AS title,
       (r->>'updatedAt') AS content_hash,
       (r->>'updatedAt')::timestamptz AS occurred_at,
       concat_ws(E'\n\n', r->>'title', r->>'description', 'Status: '||(r#>>'{state,name}')) AS body,
       r || jsonb_build_object('project', jsonb_build_object('name', p->>'name')) AS props
FROM rvbbit.mcp_rows('linear','linear_getProjects','{}'::jsonb) p
CROSS JOIN LATERAL rvbbit.mcp_rows('linear','linear_getProjectIssues',
    jsonb_build_object('projectId', p->>'id', 'limit', 250)) r
```
`doc_type='ticket'`; edge_map: team/project/assignee/label/parent/cycle. Caveat: project-less issues
aren't covered by a by-project fan-out.

---

## Worked example: Fireflies meetings (`fireflies-meetings`, complete offset pagination)

### The tool shape (`fireflies_get_transcripts`, `format:json`)
`mcp_rows` unwraps to **one row per transcript**, each:

```
id              string                          → uri  'fireflies:'||id
title           string
dateString      string (ISO, e.g. 2026-06-18T17:30:00.000Z)  → occurred_at, casts directly
organizerEmail  string (email)                  → edge: organized_by → person
participants    array<string> (emails)          → edge: attended_by → person
meetingAttendees array<{email, displayName}>
duration        number
meetingInfo, meetingLink  object/string
summary         object {
   short_summary  string                         ┐
   action_items   string                         ├─ the body (NOT the full sentences — those need
   keywords       array<string>                  ┘   fireflies_get_transcript, and are huge/noisy)
}                                                    keywords → edge: about → topic
```
There are **no `sentences`** in `get_transcripts` — it's metadata + summary, which is the ideal body.
Use `format:json` (default `toon` is unparseable). `limit` is at most 50; `skip` is the pagination
offset.

### Mapping
- `uri` = `'fireflies:'||id`; `content_hash` = `md5(summary.short_summary || summary.action_items)`
  (immutable meeting, but re-summarization should re-sync); `occurred_at` = `dateString::timestamptz`.
- `body` = title + date + organizer + participants + `summary.short_summary` + `action_items` + `keywords`.
- `doc_type='meeting'`; edge_map:
  ```json
  [{"predicate":"organized_by","kind":"person","path":"$.organizerEmail"},
   {"predicate":"attended_by","kind":"person","path":"$.participants[*]"},
   {"predicate":"about","kind":"topic","path":"$.summary.keywords[*]"}]
  ```
  (Minor noise: Google Calendar resource emails `…@resource.calendar.google.com` show as `attended_by`
  people; filter if it matters.)

### Complete pagination without destructive absence semantics

The old provider sampled two 50-item date frontiers. That made repeated **Index** clicks appear to grow
the corpus in arbitrary steps and made a partial upstream response dangerous. The shipped provider now
walks `skip=0,50,100,...` until a short page, deduplicates by transcript ID, and preserves the established
summary/action-item content hash so an upgrade does not re-embed historical meetings.

Query/MCP sources also default to `{"tombstone_missing":false}`. A successful partial window may add or
change documents, but absence cannot erase an older document. Only a provider that truly returns a
complete authoritative snapshot should explicitly opt into `tombstone_missing=true`.

```sql
WITH RECURSIVE pages(skip_n,items,n) AS (
  SELECT 0,page.items,jsonb_array_length(page.items)
    FROM LATERAL (
      SELECT coalesce(jsonb_agg(r),'[]'::jsonb) AS items
        FROM rvbbit.mcp_rows('fireflies','fireflies_get_transcripts',
             jsonb_build_object('limit',50,'skip',0,'format','json')) r
    ) page
  UNION ALL
  SELECT pages.skip_n+50,page.items,jsonb_array_length(page.items)
    FROM pages
    CROSS JOIN LATERAL (
      SELECT coalesce(jsonb_agg(r),'[]'::jsonb) AS items
        FROM rvbbit.mcp_rows('fireflies','fireflies_get_transcripts',
             jsonb_build_object('limit',50,'skip',pages.skip_n+50,'format','json')) r
    ) page
   WHERE pages.n=50 AND pages.skip_n+50 < 10000
), raw AS (
  SELECT item AS r FROM pages CROSS JOIN LATERAL jsonb_array_elements(items) item
)
SELECT 'fireflies:'||(r->>'id') AS uri,
       concat_ws(' · ', nullif(r->>'title',''), to_char((r->>'dateString')::timestamptz,'YYYY-MM-DD')) AS title,
       md5(coalesce(r#>>'{summary,short_summary}','')||coalesce(r#>>'{summary,action_items}','')) AS content_hash,
       (r->>'dateString')::timestamptz AS occurred_at,
       concat_ws(E'\n\n', r->>'title',
         nullif('Organizer: '||(r->>'organizerEmail'),'Organizer: '),
         nullif('Participants: '||array_to_string(ARRAY(SELECT jsonb_array_elements_text(r->'participants')),', '),'Participants: '),
         nullif('Summary: '||(r#>>'{summary,short_summary}'),'Summary: '),
         nullif('Action items: '||(r#>>'{summary,action_items}'),'Action items: '),
         nullif('Keywords: '||array_to_string(ARRAY(SELECT jsonb_array_elements_text(r->'summary'->'keywords')),', '),'Keywords: ')) AS body,
       r AS props
  FROM raw
```
