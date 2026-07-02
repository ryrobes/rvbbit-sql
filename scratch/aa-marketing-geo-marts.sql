CREATE EXTENSION IF NOT EXISTS cube;
CREATE EXTENSION IF NOT EXISTS earthdistance;

CREATE SCHEMA IF NOT EXISTS marketing;

DROP MATERIALIZED VIEW IF EXISTS marketing.geo_demand_clusters CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.location_radius_summary CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_origin_opportunities CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_demand_origins_zip CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_enrollment_origins_zip CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_lead_origins_zip CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_active_locations CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_zip_centroids CASCADE;

CREATE MATERIALIZED VIEW marketing.geo_zip_centroids AS
WITH ranked AS (
  SELECT DISTINCT ON (name)
    name::text AS zip,
    address_city_s::text AS city,
    address_state_code_s::text AS state,
    geo_location_latitude_s::float8 AS lat,
    geo_location_longitude_s::float8 AS lon,
    last_modified_date AS source_updated_at
  FROM salesforce.zip_code_geo_c
  WHERE COALESCE(is_deleted, false) = false
    AND name ~ '^[0-9]{5}$'
    AND geo_location_latitude_s IS NOT NULL
    AND geo_location_longitude_s IS NOT NULL
    AND geo_location_latitude_s::float8 BETWEEN -90 AND 90
    AND geo_location_longitude_s::float8 BETWEEN -180 AND 180
  ORDER BY name, last_modified_date DESC NULLS LAST
)
SELECT
  zip,
  city,
  state,
  lat,
  lon,
  ll_to_earth(lat, lon) AS earth,
  source_updated_at
FROM ranked;

CREATE MATERIALIZED VIEW marketing.geo_active_locations AS
WITH located AS (
  SELECT
    h.location_id::text AS location_id,
    h.location_name::text AS title,
    h.location_program_type::text AS course_type,
    h.location_status_normalized::text AS status,
    h.location_city::text AS city,
    h.location_state::text AS state,
    left(regexp_replace(COALESCE(h.location_postal_code, l.postal_code, ''), '[^0-9]', '', 'g'), 5) AS zip,
    COALESCE(l.latitude::float8, z.lat) AS lat,
    COALESCE(l.longitude::float8, z.lon) AS lon,
    CASE
      WHEN l.latitude IS NOT NULL AND l.longitude IS NOT NULL THEN 'location_lat_lon'
      WHEN z.zip IS NOT NULL THEN 'zip_centroid'
      ELSE 'missing'
    END AS geocode_source,
    COALESCE(h.active_course_records, 0)::int AS active_courses,
    COALESCE(h.enrolled_students, 0)::int AS enrolled_students,
    COALESCE(h.marketing_ready_flag, false) AS marketing_ready,
    l.id::text AS application_location_id,
    l.external_id::text AS location_external_id
  FROM cubes.host_location_health_cube h
  LEFT JOIN application_data_public.locations l
    ON l.external_id::text = h.location_id::text
  LEFT JOIN marketing.geo_zip_centroids z
    ON z.zip = left(regexp_replace(COALESCE(h.location_postal_code, l.postal_code, ''), '[^0-9]', '', 'g'), 5)
  WHERE h.location_status_normalized = 'active'
    AND h.location_program_type IN ('dental', 'pharmacy')
),
deduped AS (
  SELECT DISTINCT ON (location_id, course_type)
    location_id,
    title,
    course_type,
    status,
    city,
    state,
    zip,
    lat,
    lon,
    geocode_source,
    active_courses,
    enrolled_students,
    marketing_ready,
    application_location_id,
    location_external_id
  FROM located
  WHERE lat IS NOT NULL
    AND lon IS NOT NULL
    AND lat BETWEEN -90 AND 90
    AND lon BETWEEN -180 AND 180
  ORDER BY location_id, course_type, active_courses DESC, enrolled_students DESC, title
)
SELECT
  location_id,
  COALESCE(NULLIF(title, ''), 'Location') AS title,
  course_type,
  status,
  city,
  state,
  zip,
  lat,
  lon,
  ll_to_earth(lat, lon) AS earth,
  geocode_source,
  active_courses,
  enrolled_students,
  marketing_ready,
  application_location_id,
  location_external_id
FROM deduped;

CREATE MATERIALIZED VIEW marketing.geo_lead_origins_zip AS
WITH lifecycle AS (
  SELECT
    lead_id,
    max(COALESCE(course_program_type, location_program_type)) FILTER (
      WHERE COALESCE(course_program_type, location_program_type) IN ('dental', 'pharmacy')
    ) AS course_type,
    bool_or(COALESCE(became_student_or_converted, false)) AS became_student_or_converted
  FROM cubes.salesforce_lead_lifecycle_cube
  GROUP BY lead_id
),
lead_zips AS (
  SELECT
    left(regexp_replace(COALESCE(l.postal_code, ''), '[^0-9]', '', 'g'), 5) AS zip,
    COALESCE(lc.course_type, 'unknown')::text AS course_type,
    count(*)::int AS lead_count,
    count(*) FILTER (
      WHERE l.created_date >= (CURRENT_DATE - INTERVAL '365 days')
    )::int AS lead_count_365d,
    count(*) FILTER (
      WHERE COALESCE(l.is_converted, false) OR COALESCE(lc.became_student_or_converted, false)
    )::int AS converted_count,
    min(l.created_date)::date AS first_lead_date,
    max(l.created_date)::date AS last_lead_date
  FROM salesforce_quickstart.stg_salesforce__lead l
  LEFT JOIN lifecycle lc ON lc.lead_id = l.lead_id
  WHERE COALESCE(l.is_deleted, false) = false
  GROUP BY 1, 2
)
SELECT
  z.zip,
  lz.course_type,
  z.city,
  z.state,
  z.lat,
  z.lon,
  z.earth,
  lz.lead_count,
  lz.lead_count_365d,
  lz.converted_count,
  CASE WHEN lz.lead_count > 0 THEN round((lz.converted_count::numeric / lz.lead_count::numeric) * 100.0, 2) ELSE 0 END AS conversion_rate_pct,
  lz.first_lead_date,
  lz.last_lead_date
FROM lead_zips lz
JOIN marketing.geo_zip_centroids z ON z.zip = lz.zip
WHERE lz.zip <> '';

CREATE MATERIALIZED VIEW marketing.geo_enrollment_origins_zip AS
WITH student_zips AS (
  SELECT
    left(regexp_replace(COALESCE(s.postal_code, ''), '[^0-9]', '', 'g'), 5) AS zip,
    CASE COALESCE(c.program_type, -1)
      WHEN 0 THEN 'dental'
      WHEN 1 THEN 'pharmacy'
      ELSE 'unknown'
    END AS course_type,
    count(*)::int AS enrollment_count,
    count(*) FILTER (
      WHERE s.enrolled_at >= (CURRENT_DATE - INTERVAL '365 days')
    )::int AS enrollment_count_365d,
    count(*) FILTER (
      WHERE lower(COALESCE(s.status, '')) IN ('active', 'enrolled', 'graduate', 'graduated')
    )::int AS active_or_grad_count,
    min(s.enrolled_at)::date AS first_enrolled_at,
    max(s.enrolled_at)::date AS last_enrolled_at
  FROM application_data_public.students s
  LEFT JOIN application_data_public.courses c ON c.id = s.course_id
  WHERE s.enrolled_at IS NOT NULL
  GROUP BY 1, 2
)
SELECT
  z.zip,
  sz.course_type,
  z.city,
  z.state,
  z.lat,
  z.lon,
  z.earth,
  sz.enrollment_count,
  sz.enrollment_count_365d,
  sz.active_or_grad_count,
  sz.first_enrolled_at,
  sz.last_enrolled_at
FROM student_zips sz
JOIN marketing.geo_zip_centroids z ON z.zip = sz.zip
WHERE sz.zip <> '';

CREATE MATERIALIZED VIEW marketing.geo_demand_origins_zip AS
SELECT
  COALESCE(l.zip, e.zip) AS zip,
  COALESCE(l.course_type, e.course_type) AS course_type,
  COALESCE(l.city, e.city) AS city,
  COALESCE(l.state, e.state) AS state,
  COALESCE(l.lat, e.lat) AS lat,
  COALESCE(l.lon, e.lon) AS lon,
  COALESCE(l.earth, e.earth) AS earth,
  COALESCE(l.lead_count, 0)::int AS lead_count,
  COALESCE(l.lead_count_365d, 0)::int AS lead_count_365d,
  COALESCE(l.converted_count, 0)::int AS converted_count,
  COALESCE(e.enrollment_count, 0)::int AS enrollment_count,
  COALESCE(e.enrollment_count_365d, 0)::int AS enrollment_count_365d,
  COALESCE(e.active_or_grad_count, 0)::int AS active_or_grad_count,
  CASE
    WHEN COALESCE(l.lead_count, 0) > 0
      THEN round((COALESCE(l.converted_count, 0)::numeric / l.lead_count::numeric) * 100.0, 2)
    ELSE 0
  END AS conversion_rate_pct,
  CASE
    WHEN COALESCE(l.lead_count, 0) > 0
      THEN round((COALESCE(e.enrollment_count, 0)::numeric / l.lead_count::numeric) * 100.0, 2)
    ELSE 0
  END AS lead_to_enrollment_rate_pct,
  (COALESCE(l.lead_count, 0)::numeric + COALESCE(e.enrollment_count, 0)::numeric * 5.0)::numeric(18,2) AS demand_score,
  GREATEST(l.last_lead_date, e.last_enrolled_at) AS last_activity_date
FROM marketing.geo_lead_origins_zip l
FULL OUTER JOIN marketing.geo_enrollment_origins_zip e
  ON e.zip = l.zip
 AND e.course_type = l.course_type;

CREATE MATERIALIZED VIEW marketing.geo_origin_opportunities AS
WITH enriched AS (
  SELECT
    d.*,
    nearest_any.location_id AS nearest_location_id,
    nearest_any.title AS nearest_location_title,
    nearest_any.course_type AS nearest_location_course_type,
    nearest_any.distance_miles AS nearest_location_miles,
    nearest_match.location_id AS nearest_matching_location_id,
    nearest_match.title AS nearest_matching_location_title,
    nearest_match.distance_miles AS nearest_matching_location_miles,
    radius_counts.locations_25_miles,
    radius_counts.locations_50_miles
  FROM marketing.geo_demand_origins_zip d
  LEFT JOIN LATERAL (
    SELECT
      l.location_id,
      l.title,
      l.course_type,
      round((earth_distance(d.earth, l.earth) / 1609.344)::numeric, 2) AS distance_miles
    FROM marketing.geo_active_locations l
    ORDER BY earth_distance(d.earth, l.earth)
    LIMIT 1
  ) nearest_any ON true
  LEFT JOIN LATERAL (
    SELECT
      l.location_id,
      l.title,
      round((earth_distance(d.earth, l.earth) / 1609.344)::numeric, 2) AS distance_miles
    FROM marketing.geo_active_locations l
    WHERE l.course_type = d.course_type
    ORDER BY earth_distance(d.earth, l.earth)
    LIMIT 1
  ) nearest_match ON d.course_type IN ('dental', 'pharmacy')
  LEFT JOIN LATERAL (
    SELECT
      count(*) FILTER (
        WHERE earth_distance(d.earth, l.earth) <= 25.0 * 1609.344
          AND (d.course_type NOT IN ('dental', 'pharmacy') OR l.course_type = d.course_type)
      )::int AS locations_25_miles,
      count(*) FILTER (
        WHERE earth_distance(d.earth, l.earth) <= 50.0 * 1609.344
          AND (d.course_type NOT IN ('dental', 'pharmacy') OR l.course_type = d.course_type)
      )::int AS locations_50_miles
    FROM marketing.geo_active_locations l
  ) radius_counts ON true
)
SELECT
  *,
  CASE
    WHEN course_type IN ('dental', 'pharmacy')
      THEN COALESCE(nearest_matching_location_miles, 9999)
    ELSE COALESCE(nearest_location_miles, 9999)
  END AS strategy_distance_miles,
  CASE
    WHEN course_type IN ('dental', 'pharmacy')
      AND lead_count >= 50
      AND COALESCE(nearest_matching_location_miles, 9999) > 50 THEN 'white_space'
    WHEN lead_count >= 50
      AND lead_to_enrollment_rate_pct < 3 THEN 'conversion_gap'
    WHEN course_type IN ('dental', 'pharmacy')
      AND enrollment_count >= 10
      AND COALESCE(nearest_matching_location_miles, 9999) > 25 THEN 'enrollment_pull'
    WHEN course_type IN ('dental', 'pharmacy')
      AND lead_count >= 25
      AND COALESCE(nearest_matching_location_miles, 9999) > 25 THEN 'edge_market'
    ELSE 'served_or_low_signal'
  END AS market_signal,
  round(
    demand_score * CASE
      WHEN course_type IN ('dental', 'pharmacy') AND COALESCE(nearest_matching_location_miles, 9999) > 50 THEN 1.35
      WHEN course_type IN ('dental', 'pharmacy') AND COALESCE(nearest_matching_location_miles, 9999) > 25 THEN 1.15
      WHEN lead_to_enrollment_rate_pct < 3 AND lead_count >= 50 THEN 1.20
      ELSE 0.85
    END,
    2
  ) AS opportunity_score
FROM enriched;

CREATE MATERIALIZED VIEW marketing.location_radius_summary AS
WITH radii(radius_miles) AS (
  VALUES (10::int), (25::int), (50::int), (100::int)
),
scopes(course_scope) AS (
  VALUES ('all'::text), ('dental'::text), ('pharmacy'::text)
)
SELECT
  l.location_id,
  l.title,
  l.course_type AS location_course_type,
  l.city,
  l.state,
  l.zip,
  l.lat,
  l.lon,
  s.course_scope,
  r.radius_miles,
  count(d.zip)::int AS demand_zip_count,
  COALESCE(sum(d.lead_count), 0)::int AS lead_count,
  COALESCE(sum(d.lead_count_365d), 0)::int AS lead_count_365d,
  COALESCE(sum(d.converted_count), 0)::int AS converted_count,
  COALESCE(sum(d.enrollment_count), 0)::int AS enrollment_count,
  COALESCE(sum(d.enrollment_count_365d), 0)::int AS enrollment_count_365d,
  COALESCE(sum(d.active_or_grad_count), 0)::int AS active_or_grad_count,
  CASE
    WHEN COALESCE(sum(d.lead_count), 0) > 0
      THEN round((COALESCE(sum(d.enrollment_count), 0)::numeric / sum(d.lead_count)::numeric) * 100.0, 2)
    ELSE 0
  END AS lead_to_enrollment_rate_pct,
  COALESCE(sum(d.demand_score), 0)::numeric(18,2) AS demand_score,
  COALESCE(sum(o.opportunity_score), 0)::numeric(18,2) AS opportunity_score,
  count(*) FILTER (WHERE o.market_signal = 'white_space')::int AS white_space_zips,
  count(*) FILTER (WHERE o.market_signal = 'conversion_gap')::int AS conversion_gap_zips,
  count(*) FILTER (WHERE o.market_signal = 'enrollment_pull')::int AS enrollment_pull_zips,
  count(*) FILTER (WHERE o.market_signal = 'edge_market')::int AS edge_market_zips
FROM marketing.geo_active_locations l
CROSS JOIN radii r
CROSS JOIN scopes s
LEFT JOIN marketing.geo_demand_origins_zip d
  ON (s.course_scope = 'all' OR d.course_type = s.course_scope)
 AND earth_box(l.earth, r.radius_miles::float8 * 1609.344) @> d.earth
 AND earth_distance(l.earth, d.earth) <= r.radius_miles::float8 * 1609.344
LEFT JOIN marketing.geo_origin_opportunities o
  ON o.zip = d.zip
 AND o.course_type = d.course_type
GROUP BY
  l.location_id,
  l.title,
  l.course_type,
  l.city,
  l.state,
  l.zip,
  l.lat,
  l.lon,
  s.course_scope,
  r.radius_miles;

CREATE MATERIALIZED VIEW marketing.geo_demand_clusters AS
WITH cell_rows AS (
  SELECT
    o.*,
    floor(o.lat * 2.0) / 2.0 AS cell_lat,
    floor(o.lon * 2.0) / 2.0 AS cell_lon,
    GREATEST(o.opportunity_score, 1)::numeric AS weight
  FROM marketing.geo_origin_opportunities o
  WHERE o.course_type IN ('dental', 'pharmacy')
    AND (o.lead_count > 0 OR o.enrollment_count > 0)
),
aggregated AS (
  SELECT
    course_type,
    cell_lat,
    cell_lon,
    sum(lat * weight)::float8 / sum(weight)::float8 AS lat,
    sum(lon * weight)::float8 / sum(weight)::float8 AS lon,
    count(*)::int AS zip_count,
    sum(lead_count)::int AS lead_count,
    sum(lead_count_365d)::int AS lead_count_365d,
    sum(converted_count)::int AS converted_count,
    sum(enrollment_count)::int AS enrollment_count,
    sum(enrollment_count_365d)::int AS enrollment_count_365d,
    min(strategy_distance_miles) AS nearest_matching_location_miles,
    sum(demand_score)::numeric(18,2) AS demand_score,
    sum(opportunity_score)::numeric(18,2) AS opportunity_score,
    count(*) FILTER (WHERE market_signal = 'white_space')::int AS white_space_zips,
    count(*) FILTER (WHERE market_signal = 'conversion_gap')::int AS conversion_gap_zips,
    count(*) FILTER (WHERE market_signal = 'enrollment_pull')::int AS enrollment_pull_zips,
    count(*) FILTER (WHERE market_signal = 'edge_market')::int AS edge_market_zips
  FROM cell_rows
  GROUP BY course_type, cell_lat, cell_lon
)
SELECT
  *,
  ll_to_earth(lat, lon) AS earth,
  CASE
    WHEN white_space_zips > 0 AND lead_count >= 50 THEN 'white_space_cluster'
    WHEN conversion_gap_zips > 0 AND lead_count >= 50 THEN 'conversion_gap_cluster'
    WHEN enrollment_pull_zips > 0 THEN 'enrollment_pull_cluster'
    WHEN edge_market_zips > 0 THEN 'edge_market_cluster'
    ELSE 'demand_cluster'
  END AS cluster_signal
FROM aggregated
WHERE lead_count >= 10 OR enrollment_count >= 3;

CREATE UNIQUE INDEX geo_zip_centroids_zip_idx
  ON marketing.geo_zip_centroids (zip);
CREATE INDEX geo_zip_centroids_earth_gix
  ON marketing.geo_zip_centroids USING gist (earth);

CREATE UNIQUE INDEX geo_active_locations_location_course_idx
  ON marketing.geo_active_locations (location_id, course_type);
CREATE INDEX geo_active_locations_course_idx
  ON marketing.geo_active_locations (course_type);
CREATE INDEX geo_active_locations_earth_gix
  ON marketing.geo_active_locations USING gist (earth);

CREATE UNIQUE INDEX geo_lead_origins_zip_course_idx
  ON marketing.geo_lead_origins_zip (zip, course_type);
CREATE INDEX geo_lead_origins_course_count_idx
  ON marketing.geo_lead_origins_zip (course_type, lead_count DESC);
CREATE INDEX geo_lead_origins_earth_gix
  ON marketing.geo_lead_origins_zip USING gist (earth);

CREATE UNIQUE INDEX geo_enrollment_origins_zip_course_idx
  ON marketing.geo_enrollment_origins_zip (zip, course_type);
CREATE INDEX geo_enrollment_origins_course_count_idx
  ON marketing.geo_enrollment_origins_zip (course_type, enrollment_count DESC);
CREATE INDEX geo_enrollment_origins_earth_gix
  ON marketing.geo_enrollment_origins_zip USING gist (earth);

CREATE UNIQUE INDEX geo_demand_origins_zip_course_idx
  ON marketing.geo_demand_origins_zip (zip, course_type);
CREATE INDEX geo_demand_origins_signal_idx
  ON marketing.geo_demand_origins_zip (course_type, demand_score DESC);
CREATE INDEX geo_demand_origins_earth_gix
  ON marketing.geo_demand_origins_zip USING gist (earth);

CREATE UNIQUE INDEX geo_origin_opportunities_zip_course_idx
  ON marketing.geo_origin_opportunities (zip, course_type);
CREATE INDEX geo_origin_opportunities_signal_idx
  ON marketing.geo_origin_opportunities (market_signal, opportunity_score DESC);
CREATE INDEX geo_origin_opportunities_course_score_idx
  ON marketing.geo_origin_opportunities (course_type, opportunity_score DESC);
CREATE INDEX geo_origin_opportunities_earth_gix
  ON marketing.geo_origin_opportunities USING gist (earth);

CREATE UNIQUE INDEX location_radius_summary_location_scope_radius_idx
  ON marketing.location_radius_summary (location_id, course_scope, radius_miles);
CREATE INDEX location_radius_summary_scope_score_idx
  ON marketing.location_radius_summary (course_scope, radius_miles, opportunity_score DESC);

CREATE INDEX geo_demand_clusters_course_score_idx
  ON marketing.geo_demand_clusters (course_type, opportunity_score DESC);
CREATE INDEX geo_demand_clusters_signal_idx
  ON marketing.geo_demand_clusters (cluster_signal, opportunity_score DESC);
CREATE INDEX geo_demand_clusters_earth_gix
  ON marketing.geo_demand_clusters USING gist (earth);

CREATE OR REPLACE FUNCTION marketing.refresh_geo_marts()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW marketing.geo_zip_centroids;
  REFRESH MATERIALIZED VIEW marketing.geo_active_locations;
  REFRESH MATERIALIZED VIEW marketing.geo_lead_origins_zip;
  REFRESH MATERIALIZED VIEW marketing.geo_enrollment_origins_zip;
  REFRESH MATERIALIZED VIEW marketing.geo_demand_origins_zip;
  REFRESH MATERIALIZED VIEW marketing.geo_origin_opportunities;
  REFRESH MATERIALIZED VIEW marketing.location_radius_summary;
  REFRESH MATERIALIZED VIEW marketing.geo_demand_clusters;
END;
$$;

GRANT USAGE ON SCHEMA marketing TO PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA marketing TO PUBLIC;
GRANT EXECUTE ON FUNCTION marketing.refresh_geo_marts() TO PUBLIC;
