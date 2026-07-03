CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS marketing;

DROP MATERIALIZED VIEW IF EXISTS marketing.geo_active_ad_target_polygons CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.active_ad_mapping_quality CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.active_ad_group_mappings CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_active_ad_capacity_coverage CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.location_capacity_inventory CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.active_ad_groups CASCADE;

CREATE MATERIALIZED VIEW marketing.active_ad_groups AS
WITH meta_ad_sets AS (
  SELECT DISTINCT ON (id)
    'meta'::text AS vendor,
    id::text AS platform_ad_group_id,
    name::text AS platform_ad_group_name,
    campaign_id::text AS platform_campaign_id,
    status::text AS status,
    effective_status::text AS effective_status,
    configured_status::text AS configured_status,
    updated_time AS updated_at
  FROM facebook_ads.ad_set_history
  ORDER BY id, updated_time DESC NULLS LAST
),
tiktok_ad_groups AS (
  SELECT DISTINCT ON (adgroup_id)
    'tiktok'::text AS vendor,
    adgroup_id::text AS platform_ad_group_id,
    adgroup_name::text AS platform_ad_group_name,
    campaign_id::text AS platform_campaign_id,
    operation_status::text AS status,
    secondary_status::text AS effective_status,
    NULL::text AS configured_status,
    updated_at
  FROM tiktok_ads.adgroup_history
  ORDER BY adgroup_id, updated_at DESC NULLS LAST
),
google_ad_groups AS (
  SELECT DISTINCT ON (ad_group_id)
    'google'::text AS vendor,
    ad_group_id::text AS platform_ad_group_id,
    ad_group_name::text AS platform_ad_group_name,
    campaign_id::text AS platform_campaign_id,
    ad_group_status::text AS status,
    NULL::text AS effective_status,
    NULL::text AS configured_status,
    updated_at
  FROM google_ads_google_ads_source.stg_google_ads__ad_group_history
  ORDER BY ad_group_id, updated_at DESC NULLS LAST
),
unioned AS (
  SELECT * FROM meta_ad_sets
  UNION ALL
  SELECT * FROM tiktok_ad_groups
  UNION ALL
  SELECT * FROM google_ad_groups
)
SELECT
  *,
  CASE
    WHEN vendor = 'meta' AND effective_status = 'ACTIVE' THEN true
    WHEN vendor = 'tiktok' AND status = 'ENABLE' AND effective_status = 'ADGROUP_STATUS_DELIVERY_OK' THEN true
    WHEN vendor = 'google' AND status = 'ENABLED' THEN true
    ELSE false
  END AS active_delivery
FROM unioned;

CREATE MATERIALIZED VIEW marketing.location_capacity_inventory AS
WITH loc AS (
  SELECT DISTINCT ON (h.location_code, lower(h.location_program_type))
    h.location_code::text AS location_code,
    h.location_id::text AS location_id,
    h.location_name::text AS location_name,
    lower(h.location_program_type)::text AS course_type,
    h.location_city::text AS city,
    h.location_state::text AS state,
    h.location_dma::text AS dma,
    h.location_region::text AS region,
    g.zip,
    g.lat,
    g.lon,
    ST_SetSRID(ST_MakePoint(g.lon, g.lat), 4326)::geometry(Point, 4326) AS geom,
    ST_Transform(ST_SetSRID(ST_MakePoint(g.lon, g.lat), 4326), 5070)::geometry(Point, 5070) AS geom_5070
  FROM cubes.host_location_health_cube h
  JOIN marketing.geo_active_locations g
    ON g.location_id = h.location_id::text
   AND lower(g.course_type) = lower(h.location_program_type)
  WHERE h.location_status_normalized = 'active'
    AND lower(h.location_program_type) IN ('dental', 'pharmacy')
    AND g.lat BETWEEN -90 AND 90
    AND g.lon BETWEEN -180 AND 180
  ORDER BY h.location_code, lower(h.location_program_type), h.active_course_records DESC NULLS LAST, h.location_name
),
capacity AS (
  SELECT
    location_code::text,
    lower(location_program_type)::text AS course_type,
    count(*) FILTER (WHERE course_active OR course_start_date >= CURRENT_DATE)::int AS active_future_courses,
    sum(COALESCE(course_capacity, 0)) FILTER (WHERE course_active OR course_start_date >= CURRENT_DATE)::numeric(18,2) AS seats,
    sum(COALESCE(source_enrolled_students_count, active_or_onboarding_students, 0)) FILTER (WHERE course_active OR course_start_date >= CURRENT_DATE)::numeric(18,2) AS enrolled,
    sum(COALESCE(active_seats_remaining, source_seats_remaining, GREATEST(COALESCE(course_capacity, 0) - COALESCE(source_enrolled_students_count, 0), 0), 0)) FILTER (WHERE course_active OR course_start_date >= CURRENT_DATE)::numeric(18,2) AS seats_remaining,
    min(course_start_date) FILTER (WHERE course_start_date >= CURRENT_DATE) AS next_start_date,
    max(course_end_date) FILTER (WHERE course_active OR course_start_date >= CURRENT_DATE) AS last_end_date
  FROM cubes.course_capacity_health_cube
  WHERE lower(COALESCE(location_program_type, '')) IN ('dental', 'pharmacy')
  GROUP BY location_code, lower(location_program_type)
),
bounds AS (
  SELECT
    max(date_day)::date AS max_day,
    (max(date_day)::date - INTERVAL '30 days')::date AS min_day,
    (max(date_day)::date - INTERVAL '7 days')::date AS min_7_day
  FROM reporting.fact_paid_media_location_daily_alloc
),
spend AS (
  SELECT
    f.location_code,
    sum(f.spend)::numeric(18,2) AS spend_30d,
    sum(f.spend) FILTER (WHERE f.date_day > b.min_7_day)::numeric(18,2) AS spend_7d,
    sum(f.clicks)::numeric(18,2) AS clicks_30d,
    sum(f.impressions)::numeric(18,2) AS impressions_30d,
    sum(f.conversions)::numeric(18,2) AS conversions_30d,
    sum(f.students_enrolled)::numeric(18,2) AS students_30d,
    sum(f.revenue_booked)::numeric(18,2) AS revenue_30d,
    count(DISTINCT f.platform_ad_group_id)::int AS active_ad_groups_30d,
    count(DISTINCT f.platform_campaign_id)::int AS active_campaigns_30d,
    count(*)::int AS spend_rows_30d,
    string_agg(DISTINCT f.target_mapping_status, ', ' ORDER BY f.target_mapping_status) FILTER (WHERE f.target_mapping_status IS NOT NULL) AS mapping_statuses,
    string_agg(DISTINCT f.target_mapping_method, ', ' ORDER BY f.target_mapping_method) FILTER (WHERE f.target_mapping_method IS NOT NULL) AS mapping_methods
  FROM reporting.fact_paid_media_location_daily_alloc f
  CROSS JOIN bounds b
  WHERE f.date_day > b.min_day
    AND f.date_day <= b.max_day
    AND f.location_code <> '__UNMAPPED__'
  GROUP BY f.location_code
),
top_channel AS (
  SELECT DISTINCT ON (f.location_code)
    f.location_code,
    f.channel AS top_channel,
    f.vendor AS top_vendor,
    sum(f.spend)::numeric(18,2) AS top_channel_spend_30d,
    count(DISTINCT f.platform_ad_group_id)::int AS top_channel_ad_groups_30d
  FROM reporting.fact_paid_media_location_daily_alloc f
  CROSS JOIN bounds b
  WHERE f.date_day > b.min_day
    AND f.date_day <= b.max_day
    AND f.location_code <> '__UNMAPPED__'
  GROUP BY f.location_code, f.channel, f.vendor
  ORDER BY f.location_code, sum(f.spend) DESC NULLS LAST
),
joined AS (
  SELECT
    l.location_code,
    l.location_id,
    l.location_name,
    l.course_type,
    l.city,
    l.state,
    l.zip,
    l.dma,
    l.region,
    l.lat,
    l.lon,
    l.geom,
    l.geom_5070,
    COALESCE(c.active_future_courses, 0)::int AS active_future_courses,
    COALESCE(c.seats, 0)::numeric(18,2) AS seats,
    COALESCE(c.enrolled, 0)::numeric(18,2) AS enrolled,
    COALESCE(c.seats_remaining, 0)::numeric(18,2) AS seats_remaining,
    CASE WHEN c.seats > 0 THEN round((c.seats_remaining / c.seats)::numeric, 4) ELSE NULL END AS open_seat_ratio,
    c.next_start_date,
    CASE WHEN c.next_start_date IS NOT NULL THEN (c.next_start_date - CURRENT_DATE)::int ELSE NULL END AS days_to_start,
    COALESCE(s.spend_30d, 0)::numeric(18,2) AS spend_30d,
    COALESCE(s.spend_7d, 0)::numeric(18,2) AS spend_7d,
    COALESCE(s.clicks_30d, 0)::numeric(18,2) AS clicks_30d,
    COALESCE(s.impressions_30d, 0)::numeric(18,2) AS impressions_30d,
    COALESCE(s.conversions_30d, 0)::numeric(18,2) AS conversions_30d,
    COALESCE(s.students_30d, 0)::numeric(18,2) AS students_30d,
    COALESCE(s.revenue_30d, 0)::numeric(18,2) AS revenue_30d,
    COALESCE(s.active_ad_groups_30d, 0)::int AS active_ad_groups_30d,
    COALESCE(s.active_campaigns_30d, 0)::int AS active_campaigns_30d,
    s.mapping_statuses,
    s.mapping_methods,
    tc.top_channel,
    tc.top_vendor,
    COALESCE(tc.top_channel_spend_30d, 0)::numeric(18,2) AS top_channel_spend_30d,
    COALESCE(tc.top_channel_ad_groups_30d, 0)::int AS top_channel_ad_groups_30d
  FROM loc l
  LEFT JOIN capacity c
    ON c.location_code = l.location_code
   AND c.course_type = l.course_type
  LEFT JOIN spend s
    ON s.location_code = l.location_code
  LEFT JOIN top_channel tc
    ON tc.location_code = l.location_code
),
scored AS (
  SELECT
    *,
    CASE WHEN spend_30d > 0 AND seats_remaining > 0 THEN round((spend_30d / seats_remaining)::numeric, 2) ELSE NULL END AS spend_per_open_seat,
    CASE WHEN spend_30d > 0 THEN round((revenue_30d / spend_30d)::numeric, 2) ELSE NULL END AS roas_30d,
    CASE WHEN students_30d > 0 THEN round((spend_30d / students_30d)::numeric, 2) ELSE NULL END AS cac_30d,
    CASE WHEN impressions_30d > 0 THEN round((clicks_30d / impressions_30d * 100.0)::numeric, 2) ELSE NULL END AS ctr_pct_30d
  FROM joined
)
SELECT
  *,
  CASE
    WHEN active_future_courses = 0 THEN 'capacity_unknown'
    WHEN seats_remaining <= 5 AND spend_30d >= 250 THEN 'throttle_near_full'
    WHEN spend_30d >= 500 AND COALESCE(revenue_30d, 0) = 0 THEN 'inspect_spend_no_return'
    WHEN seats_remaining >= 20 AND spend_30d < 100 THEN 'scale_to_fill'
    WHEN COALESCE(roas_30d, 0) >= 2.0 AND seats_remaining >= 10 THEN 'protect_winner'
    WHEN seats_remaining >= 20 AND COALESCE(spend_per_open_seat, 0) < 25 THEN 'undercovered_capacity'
    WHEN seats_remaining >= 10 AND COALESCE(spend_per_open_seat, 0) > 100 THEN 'heavy_spend_capacity'
    ELSE 'balanced_or_monitor'
  END AS capacity_signal,
  round((
    CASE
      WHEN active_future_courses = 0 THEN 100
      WHEN seats_remaining <= 5 AND spend_30d >= 250 THEN 1000 + spend_30d
      WHEN spend_30d >= 500 AND COALESCE(revenue_30d, 0) = 0 THEN 700 + spend_30d + seats_remaining * 5
      WHEN seats_remaining >= 20 AND spend_30d < 100 THEN 650 + seats_remaining * 8 + GREATEST(0, 100 - spend_30d)
      WHEN COALESCE(roas_30d, 0) >= 2.0 AND seats_remaining >= 10 THEN 500 + seats_remaining * 4 + COALESCE(roas_30d, 0) * 60
      WHEN seats_remaining >= 20 AND COALESCE(spend_per_open_seat, 0) < 25 THEN 400 + seats_remaining * 5
      WHEN seats_remaining >= 10 AND COALESCE(spend_per_open_seat, 0) > 100 THEN 300 + spend_per_open_seat
      ELSE 100 + seats_remaining
    END
    * CASE
        WHEN days_to_start IS NULL THEN 1.0
        WHEN days_to_start <= 21 THEN 1.8
        WHEN days_to_start <= 45 THEN 1.45
        WHEN days_to_start <= 75 THEN 1.15
        ELSE 1.0
      END
  )::numeric, 2) AS action_score
FROM scored;

CREATE MATERIALIZED VIEW marketing.geo_active_ad_capacity_coverage AS
SELECT *
FROM marketing.location_capacity_inventory;

CREATE MATERIALIZED VIEW marketing.active_ad_group_mappings AS
WITH bounds AS (
  SELECT
    max(date_day)::date AS max_day,
    (max(date_day)::date - INTERVAL '30 days')::date AS min_day
  FROM reporting.fact_paid_media_location_daily_alloc
),
spend AS (
  SELECT
    vendor,
    platform_ad_group_id,
    sum(spend)::numeric(18,2) AS spend_30d,
    sum(clicks)::numeric(18,2) AS clicks_30d,
    sum(impressions)::numeric(18,2) AS impressions_30d,
    sum(conversions)::numeric(18,2) AS conversions_30d,
    sum(students_enrolled)::numeric(18,2) AS students_30d,
    sum(revenue_booked)::numeric(18,2) AS revenue_30d
  FROM reporting.fact_paid_media_location_daily_alloc f
  CROSS JOIN bounds b
  WHERE f.date_day > b.min_day
    AND f.date_day <= b.max_day
  GROUP BY vendor, platform_ad_group_id
)
SELECT
  a.vendor,
  a.platform_ad_group_id,
  a.platform_ad_group_name,
  a.platform_campaign_id,
  a.status,
  a.effective_status,
  a.configured_status,
  a.updated_at,
  a.active_delivery,
  COALESCE(b.location_code, '__NO_BRIDGE_ROW__') AS location_code,
  COALESCE(b.mapping_status, 'no_bridge_row') AS mapping_status,
  COALESCE(b.mapping_method, 'no_bridge_row') AS mapping_method,
  COALESCE(b.allocation_required, false) AS allocation_required,
  COALESCE(b.candidate_location_count, 0)::int AS candidate_location_count,
  b.allocation_weight,
  b.targeting_evidence,
  COALESCE(s.spend_30d, 0)::numeric(18,2) AS spend_30d,
  COALESCE(s.clicks_30d, 0)::numeric(18,2) AS clicks_30d,
  COALESCE(s.impressions_30d, 0)::numeric(18,2) AS impressions_30d,
  COALESCE(s.conversions_30d, 0)::numeric(18,2) AS conversions_30d,
  COALESCE(s.students_30d, 0)::numeric(18,2) AS students_30d,
  COALESCE(s.revenue_30d, 0)::numeric(18,2) AS revenue_30d,
  CASE WHEN COALESCE(s.spend_30d, 0) > 0 THEN round((COALESCE(s.revenue_30d, 0) / s.spend_30d)::numeric, 2) ELSE NULL END AS roas_30d
FROM marketing.active_ad_groups a
LEFT JOIN marts.bridge_paid_media_target_location_v2 b
  ON b.vendor = a.vendor
 AND b.platform_ad_group_id = a.platform_ad_group_id
LEFT JOIN spend s
  ON s.vendor = a.vendor
 AND s.platform_ad_group_id = a.platform_ad_group_id
WHERE a.active_delivery;

CREATE MATERIALIZED VIEW marketing.active_ad_mapping_quality AS
SELECT
  vendor,
  platform_ad_group_id,
  max(platform_ad_group_name) AS platform_ad_group_name,
  max(platform_campaign_id) AS platform_campaign_id,
  max(status) AS status,
  max(effective_status) AS effective_status,
  max(updated_at) AS updated_at,
  string_agg(DISTINCT mapping_status, ', ' ORDER BY mapping_status) AS mapping_statuses,
  string_agg(DISTINCT mapping_method, ', ' ORDER BY mapping_method) AS mapping_methods,
  count(DISTINCT location_code) FILTER (WHERE location_code NOT IN ('__UNMAPPED__', '__NO_BRIDGE_ROW__'))::int AS mapped_location_count,
  count(*) FILTER (WHERE location_code IN ('__UNMAPPED__', '__NO_BRIDGE_ROW__'))::int AS unmapped_rows,
  max(candidate_location_count)::int AS candidate_location_count,
  max(spend_30d)::numeric(18,2) AS spend_30d,
  max(students_30d)::numeric(18,2) AS students_30d,
  max(revenue_30d)::numeric(18,2) AS revenue_30d,
  max(roas_30d)::numeric(18,2) AS roas_30d,
  CASE
    WHEN bool_or(mapping_status IN ('unmapped', 'no_bridge_row')) THEN 'fix_mapping'
    WHEN bool_or(mapping_status = 'multi_target') THEN 'review_multi_target'
    WHEN max(spend_30d) >= 500 AND COALESCE(max(revenue_30d), 0) = 0 THEN 'inspect_no_return'
    ELSE 'mapped'
  END AS mapping_action
FROM marketing.active_ad_group_mappings
GROUP BY vendor, platform_ad_group_id;

CREATE MATERIALIZED VIEW marketing.geo_active_ad_target_polygons AS
WITH target_locations AS (
  SELECT DISTINCT
    m.vendor,
    m.platform_ad_group_id,
    m.platform_ad_group_name,
    m.platform_campaign_id,
    m.status,
    m.effective_status,
    m.mapping_status,
    m.mapping_method,
    m.candidate_location_count,
    m.spend_30d,
    m.students_30d,
    m.revenue_30d,
    m.roas_30d,
    c.location_code,
    c.location_name,
    c.course_type,
    c.city,
    c.state,
    c.seats_remaining,
    c.action_score,
    c.geom_5070
  FROM marketing.active_ad_group_mappings m
  JOIN marketing.geo_active_ad_capacity_coverage c
    ON c.location_code = m.location_code
  WHERE m.location_code NOT IN ('__UNMAPPED__', '__NO_BRIDGE_ROW__')
    AND c.geom_5070 IS NOT NULL
),
grouped AS (
  SELECT
    vendor,
    platform_ad_group_id,
    max(platform_ad_group_name) AS platform_ad_group_name,
    max(platform_campaign_id) AS platform_campaign_id,
    max(status) AS status,
    max(effective_status) AS effective_status,
    string_agg(DISTINCT mapping_status, ', ' ORDER BY mapping_status) AS mapping_statuses,
    string_agg(DISTINCT mapping_method, ', ' ORDER BY mapping_method) AS mapping_methods,
    count(DISTINCT location_code)::int AS location_count,
    count(DISTINCT course_type)::int AS course_count,
    (array_agg(course_type ORDER BY seats_remaining DESC NULLS LAST))[1] AS course_type,
    sum(COALESCE(seats_remaining, 0))::numeric(18,2) AS seats_remaining,
    max(spend_30d)::numeric(18,2) AS spend_30d,
    max(students_30d)::numeric(18,2) AS students_30d,
    max(revenue_30d)::numeric(18,2) AS revenue_30d,
    max(roas_30d)::numeric(18,2) AS roas_30d,
    max(candidate_location_count)::int AS candidate_location_count,
    array_to_string(
      (array_agg(
        location_code || ' ' || location_name
        ORDER BY action_score DESC NULLS LAST, seats_remaining DESC NULLS LAST, location_code
      ))[1:6],
      ' | '
    ) AS top_locations,
    ST_CollectionExtract(
      ST_MakeValid(
        ST_UnaryUnion(
          ST_Collect(
            ST_Buffer(
              geom_5070,
              CASE
                WHEN mapping_status = 'multi_target' THEN 68000.0
                WHEN mapping_status = 'single_target' THEN 42000.0
                ELSE 26000.0
              END
            )
          )
        )
      ),
      3
    ) AS shape_5070
  FROM target_locations
  GROUP BY vendor, platform_ad_group_id
  HAVING count(DISTINCT location_code) > 0
),
shaped AS (
  SELECT *
  FROM grouped
  WHERE NOT ST_IsEmpty(shape_5070)
)
SELECT
  md5(vendor || ':' || platform_ad_group_id) AS shape_id,
  vendor,
  platform_ad_group_id,
  platform_ad_group_name,
  platform_campaign_id,
  status,
  effective_status,
  mapping_statuses,
  mapping_methods,
  location_count,
  course_type,
  seats_remaining,
  spend_30d,
  students_30d,
  revenue_30d,
  roas_30d,
  candidate_location_count,
  top_locations,
  round((ST_Area(shape_5070) / 2589988.110336)::numeric, 2) AS area_sq_miles,
  ST_Y(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lat,
  ST_X(ST_Transform(ST_PointOnSurface(shape_5070), 4326)) AS lon,
  ST_Multi(ST_Transform(ST_SimplifyPreserveTopology(shape_5070, 2500), 4326))::geometry(MultiPolygon, 4326) AS shape_geom
FROM shaped;

CREATE UNIQUE INDEX active_ad_groups_vendor_ad_group_idx
  ON marketing.active_ad_groups (vendor, platform_ad_group_id);
CREATE INDEX active_ad_groups_delivery_idx
  ON marketing.active_ad_groups (active_delivery, vendor);

CREATE UNIQUE INDEX location_capacity_inventory_location_course_idx
  ON marketing.location_capacity_inventory (location_code, course_type);
CREATE INDEX location_capacity_inventory_geom_gix
  ON marketing.location_capacity_inventory USING gist (geom);

CREATE UNIQUE INDEX geo_active_ad_capacity_coverage_location_course_idx
  ON marketing.geo_active_ad_capacity_coverage (location_code, course_type);
CREATE INDEX geo_active_ad_capacity_coverage_signal_idx
  ON marketing.geo_active_ad_capacity_coverage (capacity_signal, action_score DESC);
CREATE INDEX geo_active_ad_capacity_coverage_geom_gix
  ON marketing.geo_active_ad_capacity_coverage USING gist (geom);

CREATE INDEX active_ad_group_mappings_vendor_ad_group_idx
  ON marketing.active_ad_group_mappings (vendor, platform_ad_group_id);
CREATE INDEX active_ad_group_mappings_location_idx
  ON marketing.active_ad_group_mappings (location_code, mapping_status);

CREATE UNIQUE INDEX active_ad_mapping_quality_vendor_ad_group_idx
  ON marketing.active_ad_mapping_quality (vendor, platform_ad_group_id);
CREATE INDEX active_ad_mapping_quality_action_idx
  ON marketing.active_ad_mapping_quality (mapping_action, spend_30d DESC);

CREATE UNIQUE INDEX geo_active_ad_target_polygons_shape_idx
  ON marketing.geo_active_ad_target_polygons (shape_id);
CREATE INDEX geo_active_ad_target_polygons_vendor_idx
  ON marketing.geo_active_ad_target_polygons (vendor, spend_30d DESC);
CREATE INDEX geo_active_ad_target_polygons_shape_gix
  ON marketing.geo_active_ad_target_polygons USING gist (shape_geom);

CREATE OR REPLACE FUNCTION marketing.refresh_active_ad_capacity_marts()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW marketing.active_ad_groups;
  REFRESH MATERIALIZED VIEW marketing.location_capacity_inventory;
  REFRESH MATERIALIZED VIEW marketing.geo_active_ad_capacity_coverage;
  REFRESH MATERIALIZED VIEW marketing.active_ad_group_mappings;
  REFRESH MATERIALIZED VIEW marketing.active_ad_mapping_quality;
  REFRESH MATERIALIZED VIEW marketing.geo_active_ad_target_polygons;
END;
$$;

GRANT USAGE ON SCHEMA marketing TO PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA marketing TO PUBLIC;
GRANT EXECUTE ON FUNCTION marketing.refresh_active_ad_capacity_marts() TO PUBLIC;
