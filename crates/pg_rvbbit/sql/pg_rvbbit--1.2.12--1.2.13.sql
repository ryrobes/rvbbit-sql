-- =====================================================================
-- rvbbit 1.2.12 -> 1.2.13 : snapshot-floor-aware metadata fast paths
-- =====================================================================
-- The metadata fast paths (count/sum/avg/min/max/groupby answered from
-- rvbbit.row_groups STATS without scanning parquet) summed across ALL
-- generations, ignoring the snapshot floor (min_visible_generation). So
-- "SELECT sum(x) FROM snap_t" at latest returned the cumulative sum over every
-- retained generation, not just the latest snapshot — it even constant-folded to
-- a wrong Result. The fix: a floor-aware view the fast paths read instead of
-- rvbbit.row_groups directly. SNAPSHOT table => only the latest generation;
-- APPEND table (floor 0) => every generation (unchanged). The Rust read-path
-- helpers (rewriter.rs + scan.rs) now query rvbbit.row_groups_visible; the actual
-- parquet scan path already applied the floor (custom_scan), and diagnostic /
-- maintenance queries keep using rvbbit.row_groups.

CREATE OR REPLACE VIEW rvbbit.row_groups_visible AS
SELECT rg.*
FROM rvbbit.row_groups rg
JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
WHERE rg.generation >= t.min_visible_generation;
