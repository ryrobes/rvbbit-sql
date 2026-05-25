"""Canonical analytical query set for the cross-DB bench.

Each entry is a (name, description, sql) triple. SQL is written
in portable ANSI form where possible. Per-dialect shims live in
the runner — only if a query truly can't be expressed portably.

Add new queries here; the runner picks them up automatically.
"""

QUERIES = [
    (
        "count_all",
        "Full-table count (cold scan baseline)",
        "SELECT count(*) FROM trips",
    ),
    (
        "count_filtered",
        "Filtered count — single predicate on a low-cardinality col",
        "SELECT count(*) FROM trips WHERE passenger_count > 1",
    ),
    (
        "avg_fare",
        "Single-column aggregate",
        "SELECT avg(fare_amount) FROM trips",
    ),
    (
        "groupby_vendor",
        "Group-by small cardinality (3 vendors)",
        "SELECT vendor_id, count(*) FROM trips GROUP BY vendor_id ORDER BY 1",
    ),
    (
        "groupby_payment_avg_tip",
        "Group-by + avg + top-N",
        "SELECT payment_type, avg(tip_amount) AS avg_tip "
        "FROM trips GROUP BY payment_type ORDER BY 2 DESC NULLS LAST LIMIT 10",
    ),
    (
        "daily_trip_count",
        "Group-by date — high-cardinality temporal key",
        "SELECT cast(tpep_pickup_datetime AS date) AS day, count(*) AS trips "
        "FROM trips GROUP BY 1 ORDER BY 1",
    ),
    (
        "compound_filter",
        "Two predicates + scan",
        "SELECT count(*) FROM trips "
        "WHERE passenger_count > 2 AND fare_amount > 20",
    ),
    (
        "top_routes",
        "Two-column group-by with top-N (high cardinality)",
        "SELECT pu_location_id, do_location_id, count(*) AS n "
        "FROM trips GROUP BY 1, 2 ORDER BY n DESC LIMIT 20",
    ),
    (
        "wide_agg",
        "Multi-column aggregate over filtered set",
        "SELECT avg(trip_distance), avg(fare_amount), avg(tip_amount), "
        "       sum(total_amount), max(trip_distance) "
        "FROM trips WHERE tpep_pickup_datetime >= '2023-02-01' "
        "  AND tpep_pickup_datetime <  '2023-03-01'",
    ),
]


# Rvbbit-only stats-pushdown aggregates. Equivalent SQL exists on every
# system, but rvbbit's helper functions answer from row-group metadata
# (microseconds) instead of scanning rows. Other systems get N/A here
# (they could implement the same thing but don't expose it as a UDF).
STATS_PUSHDOWN_QUERIES = [
    (
        "stats_count",
        "count(*) via row-group meta",
        "SELECT rvbbit.agg_count('trips'::regclass)",
    ),
    (
        "stats_avg_fare",
        "avg(fare_amount) via row-group stats (vs avg_fare row-scan)",
        "SELECT rvbbit.agg_avg('trips'::regclass, 'fare_amount')",
    ),
    (
        "stats_min_fare",
        "min(fare_amount) via row-group stats",
        "SELECT rvbbit.agg_min('trips'::regclass, 'fare_amount')",
    ),
    (
        "stats_max_fare",
        "max(fare_amount) via row-group stats",
        "SELECT rvbbit.agg_max('trips'::regclass, 'fare_amount')",
    ),
    (
        "stats_sum_tip",
        "sum(tip_amount) via row-group stats",
        "SELECT rvbbit.agg_sum('trips'::regclass, 'tip_amount')",
    ),
    (
        "stats_groupby_vendor_count",
        "GROUP BY vendor_id count(*) via per-group stats",
        "SELECT * FROM rvbbit.agg_groupby_count('trips'::regclass, 'vendor_id')",
    ),
    (
        "stats_groupby_payment_avg_tip",
        "GROUP BY payment_type avg(tip_amount) via per-group stats",
        "SELECT * FROM rvbbit.agg_groupby_avg('trips'::regclass, 'payment_type', 'tip_amount')",
    ),
    (
        "stats_groupby_ratecode_sum_fare",
        "GROUP BY ratecode_id sum(fare_amount) via per-group stats",
        "SELECT * FROM rvbbit.agg_groupby_sum('trips'::regclass, 'ratecode_id', 'fare_amount')",
    ),
]


# Queries only rvbbit can answer (others will show N/A in the table).
# These reference operators we'll register at bench-run time pointing at
# the sentiment + classify sidecars. The runner installs / cleans up.
SEMANTIC_QUERIES = [
    (
        "semantic_sentiment_call",
        "Per-row LLM-free sentiment via specialist sidecar",
        # Use the bigfoot_sample table from the LLM bench — has text data.
        # Falls back to NULL if the table doesn't exist.
        "SELECT rvbbit.sentiment_bigfoot(observed) FROM bigfoot_sample",
    ),
]
