\set ON_ERROR_STOP on
\pset pager off
\timing on

\echo
\echo ====================================================================
\echo 01. Dataset shape: all fields loaded, notebook table cleaned
\echo ====================================================================

SELECT
    count(*) AS full_rows,
    count(*) FILTER (WHERE NULLIF(observed, '') IS NOT NULL) AS rows_with_observed,
    count(DISTINCT state) AS states,
    count(DISTINCT county) AS counties,
    min(NULLIF(fixed_year, '')::int) FILTER (WHERE fixed_year ~ '^[0-9]+$' AND fixed_year::int > 1) AS min_year,
    max(NULLIF(fixed_year, '')::int) FILTER (WHERE fixed_year ~ '^[0-9]+$') AS max_year
FROM bigfoot.sightings_all;

SELECT state, count(*) AS reports
FROM bigfoot.sightings_all
GROUP BY state
ORDER BY reports DESC, state
LIMIT 12;

SELECT class, count(*) AS reports
FROM bigfoot.sightings_all
GROUP BY class
ORDER BY reports DESC, class;

SELECT season, count(*) AS reports
FROM bigfoot.sightings_all
GROUP BY season
ORDER BY reports DESC, season;

SELECT field, present_rows
FROM (
    SELECT 'locationdetails' AS field, count(*) FILTER (WHERE NULLIF(locationdetails, '') IS NOT NULL) AS present_rows FROM bigfoot.sightings_all
    UNION ALL SELECT 'nearesttown', count(*) FILTER (WHERE NULLIF(nearesttown, '') IS NOT NULL) FROM bigfoot.sightings_all
    UNION ALL SELECT 'nearestroad', count(*) FILTER (WHERE NULLIF(nearestroad, '') IS NOT NULL) FROM bigfoot.sightings_all
    UNION ALL SELECT 'alsonoticed', count(*) FILTER (WHERE NULLIF(alsonoticed, '') IS NOT NULL) FROM bigfoot.sightings_all
    UNION ALL SELECT 'otherwitnesses', count(*) FILTER (WHERE NULLIF(otherwitnesses, '') IS NOT NULL) FROM bigfoot.sightings_all
    UNION ALL SELECT 'otherstories', count(*) FILTER (WHERE NULLIF(otherstories, '') IS NOT NULL) FROM bigfoot.sightings_all
    UNION ALL SELECT 'timeandconditions', count(*) FILTER (WHERE NULLIF(timeandconditions, '') IS NOT NULL) FROM bigfoot.sightings_all
    UNION ALL SELECT 'environment', count(*) FILTER (WHERE NULLIF(environment, '') IS NOT NULL) FROM bigfoot.sightings_all
) coverage
ORDER BY present_rows DESC, field;

SELECT
    bfroid,
    state,
    county,
    title,
    substring(regexp_replace(report_text, '\s+', ' ', 'g'), 1, 500) AS report_text_preview
FROM bigfoot.sighting_docs
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
LIMIT 3;
