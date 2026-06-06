-- Phase 3 native+vortex read-path A/B validation.
-- Pin the native engine so we compare the READ PATH (parquet vs vortex), not the router.
-- Toggling rvbbit.native_vortex swaps which files the native scan reads.
\pset pager off
\set ON_ERROR_STOP on
SET rvbbit.route_force_candidate = 'rvbbit_native';

\echo === layout under native_vortex=off (expect: scan) ===
SET rvbbit.native_vortex = off;
EXPLAIN (COSTS off) SELECT count(*) FROM hits;

\echo === layout under native_vortex=on (expect: vortex_scan) ===
SET rvbbit.native_vortex = on;
EXPLAIN (COSTS off) SELECT count(*) FROM hits;

-- ---- baseline: native + parquet ----
SET rvbbit.native_vortex = off;
\o /tmp/nv_parquet.txt
SELECT count(*) AS cnt,
       sum(("WatchID" % 100000)) AS s_watch,
       sum("CounterID")          AS s_counter,
       avg("ResolutionWidth")::numeric(24,8) AS avg_resw,
       min("EventTime")          AS min_et,
       max("EventTime")          AS max_et,
       min("EventDate")          AS min_ed,
       max("EventDate")          AS max_ed,
       sum(length("Title"))      AS s_title_len,
       sum(length("URL"))        AS s_url_len,
       count(*) FILTER (WHERE "Title" <> '')  AS nonempty_titles,
       md5(string_agg(md5("Title"), '' ORDER BY "WatchID"))     AS title_digest,
       md5(string_agg("EventTime"::text, '' ORDER BY "WatchID")) AS et_digest
FROM hits;
SELECT "CounterID", count(*) c, sum("ResolutionWidth") rw, min("EventTime") mn
FROM hits
WHERE "EventTime" >= TIMESTAMP '2013-07-02 00:00:00' AND "CounterID" > 0
GROUP BY "CounterID" ORDER BY c DESC, "CounterID" LIMIT 25;
SELECT "WatchID", "EventTime", "EventDate", left("Title", 60) AS title, left("URL", 60) AS url, "CounterID"
FROM hits
WHERE "Title" LIKE '%Google%' ORDER BY "WatchID" LIMIT 25;
\o

-- ---- candidate: native + vortex ----
SET rvbbit.native_vortex = on;
\o /tmp/nv_vortex.txt
SELECT count(*) AS cnt,
       sum(("WatchID" % 100000)) AS s_watch,
       sum("CounterID")          AS s_counter,
       avg("ResolutionWidth")::numeric(24,8) AS avg_resw,
       min("EventTime")          AS min_et,
       max("EventTime")          AS max_et,
       min("EventDate")          AS min_ed,
       max("EventDate")          AS max_ed,
       sum(length("Title"))      AS s_title_len,
       sum(length("URL"))        AS s_url_len,
       count(*) FILTER (WHERE "Title" <> '')  AS nonempty_titles,
       md5(string_agg(md5("Title"), '' ORDER BY "WatchID"))     AS title_digest,
       md5(string_agg("EventTime"::text, '' ORDER BY "WatchID")) AS et_digest
FROM hits;
SELECT "CounterID", count(*) c, sum("ResolutionWidth") rw, min("EventTime") mn
FROM hits
WHERE "EventTime" >= TIMESTAMP '2013-07-02 00:00:00' AND "CounterID" > 0
GROUP BY "CounterID" ORDER BY c DESC, "CounterID" LIMIT 25;
SELECT "WatchID", "EventTime", "EventDate", left("Title", 60) AS title, left("URL", 60) AS url, "CounterID"
FROM hits
WHERE "Title" LIKE '%Google%' ORDER BY "WatchID" LIMIT 25;
\o
