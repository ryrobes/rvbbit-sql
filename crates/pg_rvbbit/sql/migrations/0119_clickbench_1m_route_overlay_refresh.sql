-- 0119_clickbench_1m_route_overlay_refresh
--
-- Refresh a small set of stock ClickBench route-overlay pins from the
-- 2026-07-03 1m-row forced-route comparison. These rows cover shapes where the
-- current external comparison still loses to AlloyDB/ClickHouse despite an
-- already-available RVBBit route being faster than the selected default.
--
-- This intentionally does not overwrite manual pins. Auto stock rows remain
-- self-replaceable by later stock migrations or route training.

WITH stock(shape_key, shape_family, engine, base_engine, margin_pct, sample_ms, n_samples) AS (
    VALUES
        -- Q8: RegionID top 10 unique users. Prefer GPU when available; Duck/Vortex
        -- is the best fallback in the latest forced run.
        ('native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=1|group=1|where=0|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=1|group_sig=0d2d1ea5|order_keys=<=1|order_sig=0bfe935e|cd_sig=20b57b31c828271e|width=<=16|table_rows=<=1000000|plan_join=0|subplan=0',
         'native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=1|group=1|where=0|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=1|group_sig=0d2d1ea5|order_keys=<=1|order_sig=0bfe935e|cd_sig=20b57b31c828271e|width=<=16|plan_join=0|subplan=0',
         'gpu_gqe', 'duck_vortex', 26.315789,
         '{"gpu_gqe": 14.0, "duck_vortex": 19.0, "datafusion_vector": 21.0, "rvbbit_native": 27.0, "datafusion_vortex": 35.0, "duck_vector": 46.0}', 1),

        -- Q10: MobilePhoneModel filtered count-distinct group by. DataFusion/Vortex
        -- and Duck/Vortex beat the native hard-rule at this row bucket.
        ('native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=1|group=1|where=1|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=1|group_sig=1ac1d32d|order_keys=<=1|order_sig=0bfe935e|cd_sig=20b57b31c828271e|width=<=64|table_rows=<=1000000|plan_join=0|subplan=0',
         'native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=1|group=1|where=1|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=1|group_sig=1ac1d32d|order_keys=<=1|order_sig=0bfe935e|cd_sig=20b57b31c828271e|width=<=64|plan_join=0|subplan=0',
         'datafusion_vortex', 'rvbbit_native', 10.0,
         '{"datafusion_vortex": 9.0, "duck_vortex": 9.1, "rvbbit_native": 10.0, "duck_vector": 19.0, "gpu_gqe": 21.0, "datafusion_vector": 26.0}', 1),

        -- Q11: MobilePhone+Model filtered count-distinct group by.
        ('native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=1|group=1|where=1|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=2|group_sig=1c69c3c8|order_keys=<=1|order_sig=0bfe935e|cd_sig=20b57b31c828271e|width=<=64|table_rows=<=1000000|plan_join=0|subplan=0',
         'native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=1|group=1|where=1|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=2|group_sig=1c69c3c8|order_keys=<=1|order_sig=0bfe935e|cd_sig=20b57b31c828271e|width=<=64|plan_join=0|subplan=0',
         'duck_vortex', 'rvbbit_native', 38.461538,
         '{"duck_vortex": 8.0, "datafusion_vortex": 10.0, "rvbbit_native": 13.0, "gpu_gqe": 18.0, "duck_vector": 23.0, "datafusion_vector": 27.0}', 1),

        -- Q24: first SearchPhrase values ordered by EventTime. This does not close
        -- the AlloyDB index gap, but it does avoid the slower native hard-rule.
        ('native_cap=1|tables=<=1|joins=<=0|agg=<=0|cd=<=0|group=0|where=1|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=0|group_sig=none|order_keys=<=1|order_sig=73459917|cd_sig=none|width=<=64|table_rows=<=1000000|plan_join=0|subplan=0',
         'native_cap=1|tables=<=1|joins=<=0|agg=<=0|cd=<=0|group=0|where=1|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=0|group_sig=none|order_keys=<=1|order_sig=73459917|cd_sig=none|width=<=64|plan_join=0|subplan=0',
         'duck_vortex', 'rvbbit_native', 37.5,
         '{"duck_vortex": 10.0, "datafusion_vortex": 10.1, "rvbbit_native": 16.0, "datafusion_vector": 30.0, "duck_vector": 37.0, "gpu_gqe": 44.0}', 1),

        -- Q33: URL top 10. The selected native hard-rule is materially slower
        -- here; GPU wins when available and Duck/Vortex is the best fallback.
        ('native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=0|group=1|where=0|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=1|group_sig=a8aefc37|order_keys=<=1|order_sig=2e7d2c03|cd_sig=none|width=<=64|table_rows=<=1000000|plan_join=0|subplan=0',
         'native_cap=1|tables=<=1|joins=<=0|agg=<=1|cd=<=0|group=1|where=0|order=1|limit=<=10|offset=0|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=1|group_sig=a8aefc37|order_keys=<=1|order_sig=2e7d2c03|cd_sig=none|width=<=64|plan_join=0|subplan=0',
         'gpu_gqe', 'duck_vortex', 15.384615,
         '{"gpu_gqe": 44.0, "duck_vortex": 52.0, "datafusion_vortex": 87.0, "rvbbit_native": 124.0, "datafusion_vector": 140.0, "duck_vector": 153.0}', 1),

        -- Q39: Src/Dst rollup. GPU is faster in the latest 1m forced route run;
        -- keep Duck/Vortex as the fallback for non-GPU installs.
        ('native_cap=0|tables=<=1|joins=<=0|agg=<=1|cd=<=0|group=1|where=1|order=1|limit=<=10|offset=1|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=8|group_sig=0b1188d3|order_keys=<=1|order_sig=e32679f2|cd_sig=none|width=<=256|table_rows=<=1000000|plan_join=0|subplan=0',
         'native_cap=0|tables=<=1|joins=<=0|agg=<=1|cd=<=0|group=1|where=1|order=1|limit=<=10|offset=1|star=0|like=<=0|fixed_like=<=0|regex=<=0|exists=<=0|in=<=0|between=<=0|or=<=0|group_keys=<=8|group_sig=0b1188d3|order_keys=<=1|order_sig=e32679f2|cd_sig=none|width=<=256|plan_join=0|subplan=0',
         'gpu_gqe', 'duck_vortex', 15.492958,
         '{"gpu_gqe": 60.0, "duck_vortex": 71.0, "datafusion_vortex": 97.0, "datafusion_vector": 155.0, "duck_vector": 235.0, "rvbbit_native": 331.0}', 1)
)
INSERT INTO rvbbit.route_overlay
    (shape_key, shape_family, engine, base_engine, margin_pct, sample_ms, n_samples, source, enabled)
SELECT
    shape_key, shape_family, engine, base_engine, margin_pct, sample_ms::jsonb, n_samples, 'auto', true
FROM stock
ON CONFLICT (shape_key) DO UPDATE SET
    shape_family = EXCLUDED.shape_family,
    engine = EXCLUDED.engine,
    base_engine = EXCLUDED.base_engine,
    margin_pct = EXCLUDED.margin_pct,
    sample_ms = EXCLUDED.sample_ms,
    n_samples = EXCLUDED.n_samples,
    source = 'auto',
    enabled = true,
    tested_at = now()
WHERE rvbbit.route_overlay.source = 'auto';
