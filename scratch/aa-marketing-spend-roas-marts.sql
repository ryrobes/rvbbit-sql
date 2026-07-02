CREATE SCHEMA IF NOT EXISTS marketing;

DROP MATERIALIZED VIEW IF EXISTS marketing.spend_roas_action_summary CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_spend_roas_markets CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.geo_paid_leads_zip_channel CASCADE;
DROP MATERIALIZED VIEW IF EXISTS marketing.marketing_channel_performance CASCADE;

CREATE MATERIALIZED VIEW marketing.marketing_channel_performance AS
WITH bounds AS (
  SELECT
    max(date_day)::date AS max_day,
    (max(date_day)::date - INTERVAL '180 days')::date AS min_day
  FROM marts.mart_blended_cac_by_channel
),
rolled AS (
  SELECT
    c.channel::text AS channel,
    CASE
      WHEN c.channel = 'Meta Paid Social' THEN 'paid_social_meta'
      WHEN c.channel = 'TikTok Paid Social' THEN 'paid_social_tiktok'
      WHEN c.channel = 'Google Paid Search' THEN 'paid_search_google'
      WHEN c.channel = 'Direct' THEN 'direct'
      WHEN c.channel = 'Organic Search' THEN 'organic_search'
      ELSE 'unknown'
    END AS channel_key,
    min(c.date_day)::date AS min_day,
    max(c.date_day)::date AS max_day,
    count(*)::int AS days,
    sum(c.spend)::numeric(18,2) AS spend,
    sum(c.clicks)::bigint AS clicks,
    sum(c.impressions)::bigint AS impressions,
    sum(c.leads_submitted)::bigint AS leads_submitted,
    sum(c.leads_qualified)::bigint AS leads_qualified,
    sum(c.students_enrolled)::bigint AS students_enrolled,
    sum(c.leads_converted)::bigint AS leads_converted,
    sum(c.revenue_booked)::numeric(18,2) AS revenue_booked,
    max(c.last_refreshed_at) AS last_refreshed_at
  FROM marts.mart_blended_cac_by_channel c
  CROSS JOIN bounds b
  WHERE c.date_day BETWEEN b.min_day AND b.max_day
  GROUP BY c.channel
)
SELECT
  channel,
  channel_key,
  min_day,
  max_day,
  days,
  spend,
  clicks,
  impressions,
  leads_submitted,
  leads_qualified,
  students_enrolled,
  leads_converted,
  revenue_booked,
  CASE WHEN spend > 0 THEN round(revenue_booked / spend, 2) ELSE NULL END AS roas,
  CASE WHEN students_enrolled > 0 THEN round(spend / students_enrolled::numeric, 2) ELSE NULL END AS cac,
  CASE WHEN leads_submitted > 0 THEN round(spend / leads_submitted::numeric, 2) ELSE NULL END AS cpl,
  CASE WHEN clicks > 0 THEN round(spend / clicks::numeric, 2) ELSE NULL END AS cpc,
  CASE WHEN impressions > 0 THEN round((clicks::numeric / impressions::numeric) * 100.0, 2) ELSE NULL END AS ctr_pct,
  CASE WHEN leads_submitted > 0 THEN round((students_enrolled::numeric / leads_submitted::numeric) * 100.0, 2) ELSE NULL END AS lead_to_student_pct,
  last_refreshed_at
FROM rolled;

CREATE MATERIALIZED VIEW marketing.geo_paid_leads_zip_channel AS
WITH bounds AS (
  SELECT min_day, max_day
  FROM marketing.marketing_channel_performance
  WHERE channel_key IN ('paid_social_meta', 'paid_social_tiktok', 'paid_search_google')
  LIMIT 1
),
lead_base AS (
  SELECT
    left(regexp_replace(COALESCE(l.zip_code, ''), '[^0-9]', '', 'g'), 5) AS zip,
    CASE
      WHEN lower(COALESCE(l.program_type, '')) IN ('dental', 'pharmacy') THEN lower(l.program_type)
      ELSE NULL
    END AS explicit_course_type,
    CASE
      WHEN lower(COALESCE(l.first_touch_vendor, l.vendor, '')) IN ('meta', 'facebook')
        OR lower(COALESCE(l.first_touch_channel_medium, '')) = 'paid social'
           AND lower(COALESCE(l.first_touch_vendor, l.vendor, '')) NOT IN ('tiktok')
        THEN 'paid_social_meta'
      WHEN lower(COALESCE(l.first_touch_vendor, l.vendor, '')) = 'tiktok'
        THEN 'paid_social_tiktok'
      WHEN lower(COALESCE(l.first_touch_vendor, l.vendor, '')) = 'google'
        OR lower(COALESCE(l.first_touch_channel_medium, '')) = 'paid search'
        THEN 'paid_search_google'
      ELSE NULL
    END AS channel_key,
    count(*)::numeric AS lead_count
  FROM marts.mart_unified_leads_rebuilt l
  CROSS JOIN bounds b
  WHERE COALESCE(l.is_test, false) = false
    AND l.created_at >= b.min_day::timestamp
    AND l.created_at < (b.max_day + 1)::timestamp
    AND left(regexp_replace(COALESCE(l.zip_code, ''), '[^0-9]', '', 'g'), 5) <> ''
  GROUP BY 1, 2, 3
),
zip_mix AS (
  SELECT
    zip,
    course_type,
    CASE
      WHEN sum(GREATEST(demand_score, 1)) OVER (PARTITION BY zip) > 0
        THEN GREATEST(demand_score, 1)::numeric / sum(GREATEST(demand_score, 1)) OVER (PARTITION BY zip)
      ELSE 0
    END AS course_share
  FROM marketing.geo_demand_origins_zip
  WHERE course_type IN ('dental', 'pharmacy')
),
allocated AS (
  SELECT
    lb.zip,
    COALESCE(lb.explicit_course_type, zm.course_type) AS course_type,
    lb.channel_key,
    CASE
      WHEN lb.explicit_course_type IS NOT NULL THEN lb.lead_count
      ELSE lb.lead_count * COALESCE(zm.course_share, 0)
    END AS paid_leads
  FROM lead_base lb
  LEFT JOIN zip_mix zm
    ON zm.zip = lb.zip
   AND lb.explicit_course_type IS NULL
  WHERE lb.channel_key IS NOT NULL
)
SELECT
  z.zip,
  a.course_type,
  z.city,
  z.state,
  z.lat,
  z.lon,
  z.earth,
  cp.channel,
  a.channel_key,
  round(sum(a.paid_leads), 2) AS paid_leads,
  min(cp.min_day) AS min_day,
  max(cp.max_day) AS max_day
FROM allocated a
JOIN marketing.geo_zip_centroids z ON z.zip = a.zip
JOIN marketing.marketing_channel_performance cp ON cp.channel_key = a.channel_key
WHERE a.course_type IN ('dental', 'pharmacy')
GROUP BY z.zip, a.course_type, z.city, z.state, z.lat, z.lon, z.earth, cp.channel, a.channel_key;

CREATE MATERIALIZED VIEW marketing.geo_spend_roas_markets AS
WITH channel_lead_totals AS (
  SELECT channel_key, sum(paid_leads) AS channel_paid_leads
  FROM marketing.geo_paid_leads_zip_channel
  GROUP BY channel_key
),
allocated AS (
  SELECT
    l.zip,
    l.course_type,
    l.city,
    l.state,
    l.lat,
    l.lon,
    l.earth,
    l.channel,
    l.channel_key,
    l.paid_leads,
    CASE
      WHEN clt.channel_paid_leads > 0 THEN cp.spend * (l.paid_leads / clt.channel_paid_leads)
      ELSE 0
    END AS allocated_spend,
    CASE
      WHEN clt.channel_paid_leads > 0 THEN cp.clicks::numeric * (l.paid_leads / clt.channel_paid_leads)
      ELSE 0
    END AS allocated_clicks,
    CASE
      WHEN clt.channel_paid_leads > 0 THEN cp.impressions::numeric * (l.paid_leads / clt.channel_paid_leads)
      ELSE 0
    END AS allocated_impressions,
    cp.roas AS channel_roas,
    cp.cac AS channel_cac,
    cp.cpl AS channel_cpl
  FROM marketing.geo_paid_leads_zip_channel l
  JOIN marketing.marketing_channel_performance cp ON cp.channel_key = l.channel_key
  JOIN channel_lead_totals clt ON clt.channel_key = l.channel_key
),
market_channel AS (
  SELECT
    zip,
    course_type,
    city,
    state,
    lat,
    lon,
    earth,
    channel,
    channel_key,
    paid_leads,
    allocated_spend,
    allocated_clicks,
    allocated_impressions,
    channel_roas,
    channel_cac,
    channel_cpl,
    row_number() OVER (PARTITION BY zip, course_type ORDER BY allocated_spend DESC, paid_leads DESC) AS channel_rank
  FROM allocated
),
market AS (
  SELECT
    zip,
    course_type,
    max(city) AS city,
    max(state) AS state,
    max(lat) AS lat,
    max(lon) AS lon,
    ll_to_earth(max(lat), max(lon)) AS earth,
    sum(paid_leads)::numeric(18,2) AS paid_leads,
    sum(allocated_spend)::numeric(18,2) AS allocated_spend,
    sum(allocated_clicks)::numeric(18,2) AS allocated_clicks,
    sum(allocated_impressions)::numeric(18,2) AS allocated_impressions,
    sum(allocated_spend)::numeric / NULLIF(sum(paid_leads), 0) AS allocated_cpl,
    sum(allocated_spend)::numeric / NULLIF(sum(allocated_clicks), 0) AS allocated_cpc
  FROM market_channel
  GROUP BY zip, course_type
),
top_channel AS (
  SELECT
    zip,
    course_type,
    channel AS primary_channel,
    channel_key AS primary_channel_key,
    channel_roas AS primary_channel_roas,
    channel_cac AS primary_channel_cac,
    channel_cpl AS primary_channel_cpl
  FROM market_channel
  WHERE channel_rank = 1
),
avg_value AS (
  SELECT
    sum(revenue_booked)::numeric / NULLIF(sum(students_enrolled), 0) AS avg_revenue_per_student
  FROM marketing.marketing_channel_performance
  WHERE channel_key IN ('paid_social_meta', 'paid_social_tiktok', 'paid_search_google')
),
joined AS (
  SELECT
    m.zip,
    m.course_type,
    m.city,
    m.state,
    m.lat,
    m.lon,
    m.earth,
    m.paid_leads,
    m.allocated_spend,
    m.allocated_clicks,
    m.allocated_impressions,
    round(m.allocated_cpl, 2) AS allocated_cpl,
    round(m.allocated_cpc, 2) AS allocated_cpc,
    t.primary_channel,
    t.primary_channel_key,
    t.primary_channel_roas,
    t.primary_channel_cac,
    t.primary_channel_cpl,
    o.lead_count,
    o.lead_count_365d,
    o.enrollment_count,
    o.enrollment_count_365d,
    o.lead_to_enrollment_rate_pct,
    o.opportunity_score,
    o.market_signal,
    o.strategy_distance_miles,
    o.nearest_matching_location_title,
    o.nearest_matching_location_miles,
    round((COALESCE(o.enrollment_count_365d, 0)::numeric * COALESCE(v.avg_revenue_per_student, 0))::numeric, 2) AS estimated_recent_revenue,
    CASE
      WHEN m.allocated_spend > 0
        THEN round(((COALESCE(o.enrollment_count_365d, 0)::numeric * COALESCE(v.avg_revenue_per_student, 0)) / m.allocated_spend)::numeric, 2)
      ELSE NULL
    END AS roas_proxy
  FROM market m
  JOIN marketing.geo_origin_opportunities o
    ON o.zip = m.zip
   AND o.course_type = m.course_type
  LEFT JOIN top_channel t
    ON t.zip = m.zip
   AND t.course_type = m.course_type
  CROSS JOIN avg_value v
),
scored AS (
  SELECT
    j.*,
    CASE
      WHEN sum(allocated_spend) OVER () > 0 THEN allocated_spend / sum(allocated_spend) OVER ()
      ELSE 0
    END AS spend_share,
    CASE
      WHEN sum(GREATEST(opportunity_score, 1)) OVER () > 0
        THEN GREATEST(opportunity_score, 1)::numeric / sum(GREATEST(opportunity_score, 1)) OVER ()
      ELSE 0
    END AS opportunity_share
  FROM joined j
)
SELECT
  *,
  CASE WHEN opportunity_share > 0 THEN round((spend_share / opportunity_share)::numeric, 2) ELSE NULL END AS budget_index,
  CASE
    WHEN allocated_spend >= 500 AND COALESCE(roas_proxy, 0) < 1.0 THEN 'repair_roas'
    WHEN paid_leads >= 20 AND COALESCE(enrollment_count_365d, 0) <= 2 THEN 'fix_conversion'
    WHEN opportunity_score >= 500 AND allocated_spend < 100 THEN 'test_budget'
    WHEN COALESCE(roas_proxy, 0) >= 2.0 AND (spend_share / NULLIF(opportunity_share, 0)) < 0.80 THEN 'scale_winner'
    WHEN strategy_distance_miles > 25 AND opportunity_score >= 300 THEN 'geo_expansion'
    ELSE 'monitor'
  END AS cmo_action,
  round((
    GREATEST(opportunity_score, 1)
    * CASE
        WHEN COALESCE(roas_proxy, 0) >= 2.0 THEN 1.35
        WHEN COALESCE(roas_proxy, 0) < 1.0 AND allocated_spend >= 500 THEN 0.75
        ELSE 1.0
      END
    * CASE
        WHEN opportunity_share > 0 AND (spend_share / opportunity_share) < 0.80 THEN 1.25
        WHEN opportunity_share > 0 AND (spend_share / opportunity_share) > 2.00 THEN 0.80
        ELSE 1.0
      END
  )::numeric, 2) AS cmo_priority_score
FROM scored;

CREATE MATERIALIZED VIEW marketing.spend_roas_action_summary AS
SELECT
  'all'::text AS course_type,
  cmo_action,
  count(*)::int AS markets,
  sum(paid_leads)::numeric(18,2) AS paid_leads,
  sum(allocated_spend)::numeric(18,2) AS allocated_spend,
  sum(enrollment_count_365d)::int AS enrollment_count_365d,
  sum(estimated_recent_revenue)::numeric(18,2) AS estimated_recent_revenue,
  CASE WHEN sum(allocated_spend) > 0 THEN round((sum(estimated_recent_revenue) / sum(allocated_spend))::numeric, 2) ELSE NULL END AS roas_proxy,
  sum(cmo_priority_score)::numeric(18,2) AS cmo_priority_score
FROM marketing.geo_spend_roas_markets
GROUP BY cmo_action
UNION ALL
SELECT
  course_type,
  cmo_action,
  count(*)::int AS markets,
  sum(paid_leads)::numeric(18,2) AS paid_leads,
  sum(allocated_spend)::numeric(18,2) AS allocated_spend,
  sum(enrollment_count_365d)::int AS enrollment_count_365d,
  sum(estimated_recent_revenue)::numeric(18,2) AS estimated_recent_revenue,
  CASE WHEN sum(allocated_spend) > 0 THEN round((sum(estimated_recent_revenue) / sum(allocated_spend))::numeric, 2) ELSE NULL END AS roas_proxy,
  sum(cmo_priority_score)::numeric(18,2) AS cmo_priority_score
FROM marketing.geo_spend_roas_markets
GROUP BY course_type, cmo_action;

CREATE UNIQUE INDEX marketing_channel_performance_channel_key_idx
  ON marketing.marketing_channel_performance (channel_key);
CREATE INDEX geo_paid_leads_zip_channel_key_idx
  ON marketing.geo_paid_leads_zip_channel (zip, course_type, channel_key);
CREATE INDEX geo_paid_leads_channel_idx
  ON marketing.geo_paid_leads_zip_channel (channel_key, paid_leads DESC);
CREATE INDEX geo_paid_leads_earth_gix
  ON marketing.geo_paid_leads_zip_channel USING gist (earth);

CREATE UNIQUE INDEX geo_spend_roas_markets_zip_course_idx
  ON marketing.geo_spend_roas_markets (zip, course_type);
CREATE INDEX geo_spend_roas_markets_action_idx
  ON marketing.geo_spend_roas_markets (cmo_action, cmo_priority_score DESC);
CREATE INDEX geo_spend_roas_markets_course_priority_idx
  ON marketing.geo_spend_roas_markets (course_type, cmo_priority_score DESC);
CREATE INDEX geo_spend_roas_markets_earth_gix
  ON marketing.geo_spend_roas_markets USING gist (earth);

CREATE INDEX spend_roas_action_summary_course_action_idx
  ON marketing.spend_roas_action_summary (course_type, cmo_action);

CREATE OR REPLACE FUNCTION marketing.refresh_spend_roas_marts()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW marketing.marketing_channel_performance;
  REFRESH MATERIALIZED VIEW marketing.geo_paid_leads_zip_channel;
  REFRESH MATERIALIZED VIEW marketing.geo_spend_roas_markets;
  REFRESH MATERIALIZED VIEW marketing.spend_roas_action_summary;
END;
$$;

GRANT USAGE ON SCHEMA marketing TO PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA marketing TO PUBLIC;
GRANT EXECUTE ON FUNCTION marketing.refresh_spend_roas_marts() TO PUBLIC;
