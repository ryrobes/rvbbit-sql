CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS marketing;

DROP MATERIALIZED VIEW IF EXISTS marketing.geo_cpl_cloud_polygons CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_budget_cloud_polygons CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_demand_cluster_polygons CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_spend_market_points CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_demand_origin_points CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_active_location_points CASCADE;

CREATE MATERIALIZED VIEW marketing.geo_active_location_points AS
SELECT
  l.*,
  ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326)::geometry(Point, 4326) AS geom,
  ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326)::geography(Point, 4326) AS geog,
  ST_Transform(ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326), 5070)::geometry(Point, 5070) AS geom_5070
FROM marketing.geo_active_locations l
WHERE l.lat BETWEEN -90 AND 90
  AND l.lon BETWEEN -180 AND 180;

CREATE MATERIALIZED VIEW marketing.geo_demand_origin_points AS
SELECT
  d.*,
  ST_SetSRID(ST_MakePoint(d.lon, d.lat), 4326)::geometry(Point, 4326) AS geom,
  ST_SetSRID(ST_MakePoint(d.lon, d.lat), 4326)::geography(Point, 4326) AS geog,
  ST_Transform(ST_SetSRID(ST_MakePoint(d.lon, d.lat), 4326), 5070)::geometry(Point, 5070) AS geom_5070
FROM marketing.geo_origin_opportunities d
WHERE d.lat BETWEEN -90 AND 90
  AND d.lon BETWEEN -180 AND 180;

CREATE MATERIALIZED VIEW marketing.geo_spend_market_points AS
SELECT
  s.*,
  ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326)::geometry(Point, 4326) AS geom,
  ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326)::geography(Point, 4326) AS geog,
  ST_Transform(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), 5070)::geometry(Point, 5070) AS geom_5070
FROM marketing.geo_spend_roas_markets s
WHERE s.lat BETWEEN -90 AND 90
  AND s.lon BETWEEN -180 AND 180;

CREATE MATERIALIZED VIEW marketing.geo_demand_cluster_polygons AS
WITH base AS (
  SELECT
    p.*,
    CASE
      WHEN p.market_signal = 'white_space' THEN 'white_space_cluster'
      WHEN p.market_signal = 'conversion_gap' THEN 'conversion_gap_cluster'
      WHEN p.market_signal = 'enrollment_pull' THEN 'enrollment_pull_cluster'
      WHEN p.market_signal = 'edge_market' THEN 'edge_market_cluster'
      ELSE 'demand_cluster'
    END AS cluster_signal,
    GREATEST(p.opportunity_score, 1)::numeric AS shape_weight
  FROM marketing.geo_demand_origin_points p
  WHERE p.course_type IN ('dental', 'pharmacy')
    AND (p.lead_count > 0 OR p.enrollment_count > 0)
    AND p.opportunity_score > 0
),
clustered AS (
  SELECT
    b.*,
    COALESCE(
      ST_ClusterDBSCAN(b.geom_5070, eps := 70000, minpoints := 2)
        OVER (PARTITION BY b.course_type, b.cluster_signal)::text,
      'single-' || b.zip
    ) AS cluster_id,
    LEAST(90000.0, GREATEST(22000.0, LN(b.shape_weight::float8 + 1.0) * 7000.0)) AS buffer_meters
  FROM base b
),
grouped AS (
  SELECT
    course_type,
    cluster_signal,
    cluster_id,
    (array_agg(city ORDER BY shape_weight DESC, opportunity_score DESC, lead_count DESC, zip))[1] AS anchor_city,
    (array_agg(state ORDER BY shape_weight DESC, opportunity_score DESC, lead_count DESC, zip))[1] AS anchor_state,
    (array_agg(zip ORDER BY shape_weight DESC, opportunity_score DESC, lead_count DESC, zip))[1] AS anchor_zip,
    count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '')::int AS state_count,
    array_to_string(
      (array_agg(DISTINCT state ORDER BY state) FILTER (WHERE state IS NOT NULL AND state <> ''))[1:8],
      ', '
    ) ||
      CASE
        WHEN count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '') > 8
          THEN ' +' || (count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '') - 8)::text
        ELSE ''
      END AS states,
    array_to_string(
      (array_agg(
        trim(concat_ws(' ', NULLIF(zip, ''), NULLIF(concat_ws(', ', NULLIF(city, ''), NULLIF(state, '')), '')))
        ORDER BY shape_weight DESC, opportunity_score DESC, lead_count DESC, zip
      ))[1:5],
      ' | '
    ) AS top_zips,
    count(*)::int AS zip_count,
    sum(lead_count)::int AS lead_count,
    sum(lead_count_365d)::int AS lead_count_365d,
    sum(converted_count)::int AS converted_count,
    sum(enrollment_count)::int AS enrollment_count,
    sum(enrollment_count_365d)::int AS enrollment_count_365d,
    min(strategy_distance_miles) AS nearest_matching_location_miles,
    sum(demand_score)::numeric(18,2) AS demand_score,
    sum(opportunity_score)::numeric(18,2) AS opportunity_score,
    sum(shape_weight)::numeric(18,2) AS shape_weight,
    count(*) FILTER (WHERE market_signal = 'white_space')::int AS white_space_zips,
    count(*) FILTER (WHERE market_signal = 'conversion_gap')::int AS conversion_gap_zips,
    count(*) FILTER (WHERE market_signal = 'enrollment_pull')::int AS enrollment_pull_zips,
    count(*) FILTER (WHERE market_signal = 'edge_market')::int AS edge_market_zips,
    ST_CollectionExtract(
      ST_MakeValid(ST_UnaryUnion(ST_Collect(ST_Buffer(geom_5070, buffer_meters)))),
      3
    ) AS shape_5070
  FROM clustered
  GROUP BY course_type, cluster_signal, cluster_id
  HAVING sum(lead_count) >= 10 OR sum(enrollment_count) >= 3
),
shaped AS (
  SELECT *
  FROM grouped
  WHERE NOT ST_IsEmpty(shape_5070)
)
SELECT
  md5(course_type || ':' || cluster_signal || ':' || cluster_id) AS shape_id,
  course_type,
  cluster_signal,
  cluster_id,
  anchor_city,
  anchor_state,
  anchor_zip,
  state_count,
  states,
  top_zips,
  zip_count,
  lead_count,
  lead_count_365d,
  converted_count,
  enrollment_count,
  enrollment_count_365d,
  nearest_matching_location_miles,
  demand_score,
  opportunity_score,
  shape_weight,
  white_space_zips,
  conversion_gap_zips,
  enrollment_pull_zips,
  edge_market_zips,
  round((ST_Area(shape_5070) / 2589988.110336)::numeric, 2) AS area_sq_miles,
  ST_Y(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lat,
  ST_X(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lon,
  ST_Multi(ST_Transform(ST_SimplifyPreserveTopology(shape_5070, 2500), 4326))::geometry(MultiPolygon, 4326) AS shape_geom
FROM shaped;

CREATE MATERIALIZED VIEW marketing.geo_budget_cloud_polygons AS
WITH signals AS (
  SELECT
    p.*,
    CASE
      WHEN p.cmo_action IN ('scale_winner', 'test_budget', 'geo_expansion')
        OR (p.opportunity_score >= 500 AND COALESCE(p.budget_index, 0) < 0.8)
        THEN 'underfunded'
      WHEN p.cmo_action IN ('repair_roas', 'fix_conversion')
        OR (p.allocated_spend >= 500 AND COALESCE(p.roas_proxy, 0) < 1.5)
        THEN 'overspend'
      ELSE NULL
    END AS cloud_type,
    CASE
      WHEN p.cmo_action IN ('scale_winner', 'test_budget', 'geo_expansion')
        OR (p.opportunity_score >= 500 AND COALESCE(p.budget_index, 0) < 0.8)
        THEN GREATEST(p.cmo_priority_score, p.opportunity_score, 1)
      WHEN p.cmo_action IN ('repair_roas', 'fix_conversion')
        OR (p.allocated_spend >= 500 AND COALESCE(p.roas_proxy, 0) < 1.5)
        THEN GREATEST(p.allocated_spend, p.paid_leads * 25.0, 1)
      ELSE 0
    END AS cloud_weight
  FROM marketing.geo_spend_market_points p
),
ranked AS (
  SELECT
    s.*,
    row_number() OVER (PARTITION BY cloud_type ORDER BY cloud_weight DESC) AS cloud_rank
  FROM signals s
  WHERE s.cloud_type IS NOT NULL
    AND s.cloud_weight > 0
),
clustered AS (
  SELECT
    r.*,
    COALESCE(
      ST_ClusterDBSCAN(r.geom_5070, eps := 85000, minpoints := 2)
        OVER (PARTITION BY r.course_type, r.cloud_type)::text,
      'single-' || r.zip
    ) AS cluster_id,
    LEAST(140000.0, GREATEST(35000.0, LN(r.cloud_weight::float8 + 1.0) * 8500.0)) AS buffer_meters
  FROM ranked r
  WHERE r.cloud_rank <= 650
),
grouped AS (
  SELECT
    course_type,
    cloud_type,
    cluster_id,
    (array_agg(city ORDER BY cloud_weight DESC, cmo_priority_score DESC, paid_leads DESC, zip))[1] AS anchor_city,
    (array_agg(state ORDER BY cloud_weight DESC, cmo_priority_score DESC, paid_leads DESC, zip))[1] AS anchor_state,
    (array_agg(zip ORDER BY cloud_weight DESC, cmo_priority_score DESC, paid_leads DESC, zip))[1] AS anchor_zip,
    count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '')::int AS state_count,
    array_to_string(
      (array_agg(DISTINCT state ORDER BY state) FILTER (WHERE state IS NOT NULL AND state <> ''))[1:8],
      ', '
    ) ||
      CASE
        WHEN count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '') > 8
          THEN ' +' || (count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '') - 8)::text
        ELSE ''
      END AS states,
    array_to_string(
      (array_agg(
        trim(concat_ws(' ', NULLIF(zip, ''), NULLIF(concat_ws(', ', NULLIF(city, ''), NULLIF(state, '')), '')))
        ORDER BY cloud_weight DESC, cmo_priority_score DESC, paid_leads DESC, zip
      ))[1:5],
      ' | '
    ) AS top_zips,
    count(*)::int AS market_count,
    sum(paid_leads)::numeric(18,2) AS paid_leads,
    sum(allocated_spend)::numeric(18,2) AS allocated_spend,
    sum(enrollment_count_365d)::int AS enrollment_count_365d,
    sum(estimated_recent_revenue)::numeric(18,2) AS estimated_recent_revenue,
    CASE WHEN sum(allocated_spend) > 0 THEN round((sum(estimated_recent_revenue) / sum(allocated_spend))::numeric, 2) ELSE NULL END AS roas_proxy,
    avg(budget_index)::numeric(18,2) AS budget_index,
    sum(opportunity_score)::numeric(18,2) AS opportunity_score,
    sum(cmo_priority_score)::numeric(18,2) AS cmo_priority_score,
    sum(cloud_weight)::numeric(18,2) AS cloud_weight,
    string_agg(DISTINCT cmo_action, ', ' ORDER BY cmo_action) AS cmo_actions,
    string_agg(DISTINCT primary_channel, ', ' ORDER BY primary_channel) FILTER (WHERE primary_channel IS NOT NULL) AS primary_channels,
    ST_CollectionExtract(
      ST_MakeValid(ST_UnaryUnion(ST_Collect(ST_Buffer(geom_5070, buffer_meters)))),
      3
    ) AS shape_5070
  FROM clustered
  GROUP BY course_type, cloud_type, cluster_id
  HAVING count(*) >= 2 OR sum(cloud_weight) >= 500
),
shaped AS (
  SELECT *
  FROM grouped
  WHERE NOT ST_IsEmpty(shape_5070)
)
SELECT
  md5(course_type || ':' || cloud_type || ':' || cluster_id) AS shape_id,
  course_type,
  cloud_type,
  cluster_id,
  anchor_city,
  anchor_state,
  anchor_zip,
  state_count,
  states,
  top_zips,
  market_count,
  paid_leads,
  allocated_spend,
  enrollment_count_365d,
  estimated_recent_revenue,
  roas_proxy,
  budget_index,
  opportunity_score,
  cmo_priority_score,
  cloud_weight,
  cmo_actions,
  primary_channels,
  round((ST_Area(shape_5070) / 2589988.110336)::numeric, 2) AS area_sq_miles,
  ST_Y(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lat,
  ST_X(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lon,
  ST_Multi(ST_Transform(ST_SimplifyPreserveTopology(shape_5070, 2500), 4326))::geometry(MultiPolygon, 4326) AS shape_geom
FROM shaped;

CREATE MATERIALIZED VIEW marketing.geo_cpl_cloud_polygons AS
WITH base AS (
  SELECT *
  FROM marketing.geo_spend_market_points
  WHERE allocated_cpl IS NOT NULL
    AND allocated_cpl > 0
    AND paid_leads >= 2
    AND allocated_spend > 0
),
stats AS (
  SELECT
    course_type,
    min(allocated_cpl)::numeric AS cpl_min,
    percentile_cont(0.10) WITHIN GROUP (ORDER BY allocated_cpl)::numeric AS cpl_p10,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY allocated_cpl)::numeric AS cpl_p25,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY allocated_cpl)::numeric AS median_cpl,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY allocated_cpl)::numeric AS cpl_p75,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY allocated_cpl)::numeric AS cpl_p90,
    max(allocated_cpl)::numeric AS cpl_max
  FROM base
  GROUP BY course_type
),
signals AS (
  SELECT
    b.*,
    s.cpl_min,
    s.cpl_p10,
    s.cpl_p25,
    s.median_cpl,
    s.cpl_p75,
    s.cpl_p90,
    s.cpl_max,
    round((b.allocated_cpl / NULLIF(s.median_cpl, 0))::numeric, 2) AS cpl_index,
    round(
      LEAST(1.0, GREATEST(0.0,
        CASE
          WHEN b.allocated_cpl <= s.median_cpl AND s.median_cpl > s.cpl_p10
            THEN 0.5 * ((b.allocated_cpl - s.cpl_p10) / NULLIF(s.median_cpl - s.cpl_p10, 0))
          WHEN b.allocated_cpl > s.median_cpl AND s.cpl_p90 > s.median_cpl
            THEN 0.5 + 0.5 * ((b.allocated_cpl - s.median_cpl) / NULLIF(s.cpl_p90 - s.median_cpl, 0))
          ELSE 0.5
        END
      ))::numeric,
      4
    ) AS cpl_norm,
    CASE
      WHEN LEAST(1.0, GREATEST(0.0,
        CASE
          WHEN b.allocated_cpl <= s.median_cpl AND s.median_cpl > s.cpl_p10
            THEN 0.5 * ((b.allocated_cpl - s.cpl_p10) / NULLIF(s.median_cpl - s.cpl_p10, 0))
          WHEN b.allocated_cpl > s.median_cpl AND s.cpl_p90 > s.median_cpl
            THEN 0.5 + 0.5 * ((b.allocated_cpl - s.median_cpl) / NULLIF(s.cpl_p90 - s.median_cpl, 0))
          ELSE 0.5
        END
      )) <= 0.25 THEN 'cold_cpl'
      WHEN LEAST(1.0, GREATEST(0.0,
        CASE
          WHEN b.allocated_cpl <= s.median_cpl AND s.median_cpl > s.cpl_p10
            THEN 0.5 * ((b.allocated_cpl - s.cpl_p10) / NULLIF(s.median_cpl - s.cpl_p10, 0))
          WHEN b.allocated_cpl > s.median_cpl AND s.cpl_p90 > s.median_cpl
            THEN 0.5 + 0.5 * ((b.allocated_cpl - s.median_cpl) / NULLIF(s.cpl_p90 - s.median_cpl, 0))
          ELSE 0.5
        END
      )) <= 0.50 THEN 'cool_cpl'
      WHEN LEAST(1.0, GREATEST(0.0,
        CASE
          WHEN b.allocated_cpl <= s.median_cpl AND s.median_cpl > s.cpl_p10
            THEN 0.5 * ((b.allocated_cpl - s.cpl_p10) / NULLIF(s.median_cpl - s.cpl_p10, 0))
          WHEN b.allocated_cpl > s.median_cpl AND s.cpl_p90 > s.median_cpl
            THEN 0.5 + 0.5 * ((b.allocated_cpl - s.median_cpl) / NULLIF(s.cpl_p90 - s.median_cpl, 0))
          ELSE 0.5
        END
      )) <= 0.75 THEN 'warm_cpl'
      ELSE 'hot_cpl'
    END AS cpl_cloud_type,
    GREATEST(b.allocated_cpl * b.paid_leads, b.allocated_spend, b.cmo_priority_score, 1) AS cpl_cloud_weight
  FROM base b
  JOIN stats s ON s.course_type = b.course_type
),
ranked AS (
  SELECT
    s.*,
    row_number() OVER (PARTITION BY course_type, cpl_cloud_type ORDER BY cpl_cloud_weight DESC) AS cpl_cloud_rank
  FROM signals s
  WHERE s.cpl_cloud_weight > 0
),
clustered AS (
  SELECT
    r.*,
    COALESCE(
      ST_ClusterDBSCAN(r.geom_5070, eps := 78000, minpoints := 2)
        OVER (PARTITION BY r.course_type, r.cpl_cloud_type)::text,
      'single-' || r.zip
    ) AS cluster_id,
    LEAST(130000.0, GREATEST(28000.0, LN(r.cpl_cloud_weight::float8 + 1.0) * 7200.0)) AS buffer_meters
  FROM ranked r
  WHERE r.cpl_cloud_rank <= 750
),
grouped AS (
  SELECT
    course_type,
    cpl_cloud_type,
    cluster_id,
    (array_agg(city ORDER BY cpl_cloud_weight DESC, allocated_spend DESC, paid_leads DESC, zip))[1] AS anchor_city,
    (array_agg(state ORDER BY cpl_cloud_weight DESC, allocated_spend DESC, paid_leads DESC, zip))[1] AS anchor_state,
    (array_agg(zip ORDER BY cpl_cloud_weight DESC, allocated_spend DESC, paid_leads DESC, zip))[1] AS anchor_zip,
    count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '')::int AS state_count,
    array_to_string(
      (array_agg(DISTINCT state ORDER BY state) FILTER (WHERE state IS NOT NULL AND state <> ''))[1:8],
      ', '
    ) ||
      CASE
        WHEN count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '') > 8
          THEN ' +' || (count(DISTINCT state) FILTER (WHERE state IS NOT NULL AND state <> '') - 8)::text
        ELSE ''
      END AS states,
    array_to_string(
      (array_agg(
        trim(concat_ws(' ', NULLIF(zip, ''), NULLIF(concat_ws(', ', NULLIF(city, ''), NULLIF(state, '')), '')))
        ORDER BY cpl_cloud_weight DESC, allocated_spend DESC, paid_leads DESC, zip
      ))[1:5],
      ' | '
    ) AS top_zips,
    count(*)::int AS market_count,
    sum(paid_leads)::numeric(18,2) AS paid_leads,
    sum(allocated_spend)::numeric(18,2) AS allocated_spend,
    round((sum(allocated_spend) / NULLIF(sum(paid_leads), 0))::numeric, 2) AS allocated_cpl,
    max(cpl_min)::numeric(18,2) AS cpl_min,
    max(cpl_p10)::numeric(18,2) AS cpl_p10,
    max(cpl_p25)::numeric(18,2) AS cpl_p25,
    max(median_cpl)::numeric(18,2) AS median_cpl,
    max(cpl_p75)::numeric(18,2) AS cpl_p75,
    max(cpl_p90)::numeric(18,2) AS cpl_p90,
    max(cpl_max)::numeric(18,2) AS cpl_max,
    avg(roas_proxy)::numeric(18,2) AS roas_proxy,
    sum(cmo_priority_score)::numeric(18,2) AS cmo_priority_score,
    sum(cpl_cloud_weight)::numeric(18,2) AS cpl_cloud_weight,
    string_agg(DISTINCT cmo_action, ', ' ORDER BY cmo_action) AS cmo_actions,
    string_agg(DISTINCT primary_channel, ', ' ORDER BY primary_channel) FILTER (WHERE primary_channel IS NOT NULL) AS primary_channels,
    ST_CollectionExtract(
      ST_MakeValid(ST_UnaryUnion(ST_Collect(ST_Buffer(geom_5070, buffer_meters)))),
      3
    ) AS shape_5070
  FROM clustered
  GROUP BY course_type, cpl_cloud_type, cluster_id
  HAVING count(*) >= 2 OR sum(cpl_cloud_weight) >= 500
),
shaped AS (
  SELECT
    *,
    round((allocated_cpl / NULLIF(median_cpl, 0))::numeric, 2) AS cpl_index,
    round(
      LEAST(1.0, GREATEST(0.0,
        CASE
          WHEN allocated_cpl <= median_cpl AND median_cpl > cpl_p10
            THEN 0.5 * ((allocated_cpl - cpl_p10) / NULLIF(median_cpl - cpl_p10, 0))
          WHEN allocated_cpl > median_cpl AND cpl_p90 > median_cpl
            THEN 0.5 + 0.5 * ((allocated_cpl - median_cpl) / NULLIF(cpl_p90 - median_cpl, 0))
          ELSE 0.5
        END
      ))::numeric,
      4
    ) AS cpl_norm
  FROM grouped
  WHERE NOT ST_IsEmpty(shape_5070)
)
SELECT
  md5(course_type || ':' || cpl_cloud_type || ':' || cluster_id) AS shape_id,
  course_type,
  CASE
    WHEN cpl_norm <= 0.25 THEN 'cold_cpl'
    WHEN cpl_norm <= 0.50 THEN 'cool_cpl'
    WHEN cpl_norm <= 0.75 THEN 'warm_cpl'
    ELSE 'hot_cpl'
  END AS cpl_cloud_type,
  cluster_id,
  anchor_city,
  anchor_state,
  anchor_zip,
  state_count,
  states,
  top_zips,
  market_count,
  paid_leads,
  allocated_spend,
  allocated_cpl,
  cpl_min,
  cpl_p10,
  cpl_p25,
  median_cpl,
  cpl_p75,
  cpl_p90,
  cpl_max,
  cpl_index,
  cpl_norm,
  roas_proxy,
  cmo_priority_score,
  cpl_cloud_weight,
  cmo_actions,
  primary_channels,
  round((ST_Area(shape_5070) / 2589988.110336)::numeric, 2) AS area_sq_miles,
  ST_Y(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lat,
  ST_X(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lon,
  ST_Multi(ST_Transform(ST_SimplifyPreserveTopology(shape_5070, 2500), 4326))::geometry(MultiPolygon, 4326) AS shape_geom
FROM shaped;

CREATE UNIQUE INDEX geo_active_location_points_location_course_idx
  ON marketing.geo_active_location_points (location_id, course_type);
CREATE INDEX geo_active_location_points_geog_gix
  ON marketing.geo_active_location_points USING gist (geog);
CREATE INDEX geo_active_location_points_geom_5070_gix
  ON marketing.geo_active_location_points USING gist (geom_5070);

CREATE UNIQUE INDEX geo_demand_origin_points_zip_course_idx
  ON marketing.geo_demand_origin_points (zip, course_type);
CREATE INDEX geo_demand_origin_points_course_score_idx
  ON marketing.geo_demand_origin_points (course_type, opportunity_score DESC);
CREATE INDEX geo_demand_origin_points_geog_gix
  ON marketing.geo_demand_origin_points USING gist (geog);
CREATE INDEX geo_demand_origin_points_geom_5070_gix
  ON marketing.geo_demand_origin_points USING gist (geom_5070);

CREATE UNIQUE INDEX geo_spend_market_points_zip_course_idx
  ON marketing.geo_spend_market_points (zip, course_type);
CREATE INDEX geo_spend_market_points_action_idx
  ON marketing.geo_spend_market_points (cmo_action, cmo_priority_score DESC);
CREATE INDEX geo_spend_market_points_geog_gix
  ON marketing.geo_spend_market_points USING gist (geog);
CREATE INDEX geo_spend_market_points_geom_5070_gix
  ON marketing.geo_spend_market_points USING gist (geom_5070);

CREATE UNIQUE INDEX geo_demand_cluster_polygons_shape_idx
  ON marketing.geo_demand_cluster_polygons (shape_id);
CREATE INDEX geo_demand_cluster_polygons_course_score_idx
  ON marketing.geo_demand_cluster_polygons (course_type, opportunity_score DESC);
CREATE INDEX geo_demand_cluster_polygons_signal_idx
  ON marketing.geo_demand_cluster_polygons (cluster_signal, opportunity_score DESC);
CREATE INDEX geo_demand_cluster_polygons_shape_gix
  ON marketing.geo_demand_cluster_polygons USING gist (shape_geom);

CREATE UNIQUE INDEX geo_budget_cloud_polygons_shape_idx
  ON marketing.geo_budget_cloud_polygons (shape_id);
CREATE INDEX geo_budget_cloud_polygons_course_weight_idx
  ON marketing.geo_budget_cloud_polygons (course_type, cloud_type, cloud_weight DESC);
CREATE INDEX geo_budget_cloud_polygons_shape_gix
  ON marketing.geo_budget_cloud_polygons USING gist (shape_geom);

CREATE UNIQUE INDEX geo_cpl_cloud_polygons_shape_idx
  ON marketing.geo_cpl_cloud_polygons (shape_id);
CREATE INDEX geo_cpl_cloud_polygons_course_weight_idx
  ON marketing.geo_cpl_cloud_polygons (course_type, cpl_cloud_type, cpl_cloud_weight DESC);
CREATE INDEX geo_cpl_cloud_polygons_shape_gix
  ON marketing.geo_cpl_cloud_polygons USING gist (shape_geom);

CREATE OR REPLACE FUNCTION marketing.refresh_postgis_geo_marts()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW marketing.geo_active_location_points;
  REFRESH MATERIALIZED VIEW marketing.geo_demand_origin_points;
  REFRESH MATERIALIZED VIEW marketing.geo_spend_market_points;
  REFRESH MATERIALIZED VIEW marketing.geo_demand_cluster_polygons;
  REFRESH MATERIALIZED VIEW marketing.geo_budget_cloud_polygons;
  REFRESH MATERIALIZED VIEW marketing.geo_cpl_cloud_polygons;
END;
$$;

GRANT USAGE ON SCHEMA marketing TO PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA marketing TO PUBLIC;
GRANT EXECUTE ON FUNCTION marketing.refresh_postgis_geo_marts() TO PUBLIC;
