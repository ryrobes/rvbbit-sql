\set ON_ERROR_STOP on
\pset pager off
\timing on

\if :{?sample_rows}
\else
\set sample_rows 500
\endif

\echo
\echo ====================================================================
\echo 00. Load full BFRO CSV and build the notebook working table
\echo ====================================================================
\echo CSV path: /home/ryanr/csv-files/bigfoot_sightings.csv
\echo Sample rows for semantic notebook table: :sample_rows

CREATE EXTENSION IF NOT EXISTS pg_rvbbit;

DROP SCHEMA IF EXISTS bigfoot CASCADE;
CREATE SCHEMA bigfoot;

CREATE TABLE bigfoot.sightings_all (
    bfroid text PRIMARY KEY,
    submitted text,
    submitted_date text,
    title text,
    class text,
    month text,
    fixed_month text,
    date text,
    year text,
    fixed_year text,
    season text,
    state text,
    county text,
    locationdetails text,
    nearesttown text,
    nearestroad text,
    observed text,
    alsonoticed text,
    otherwitnesses text,
    otherstories text,
    timeandconditions text,
    environment text,
    url text,
    run_id text,
    run_time text,
    sketch text
) USING rvbbit;

\copy bigfoot.sightings_all (bfroid, submitted, submitted_date, title, class, month, fixed_month, date, year, fixed_year, season, state, county, locationdetails, nearesttown, nearestroad, observed, alsonoticed, otherwitnesses, otherstories, timeandconditions, environment, url, run_id, run_time, sketch) FROM '/home/ryanr/csv-files/bigfoot_sightings.csv' WITH (FORMAT csv, HEADER true)

CREATE INDEX bigfoot_sightings_all_state_idx ON bigfoot.sightings_all (state);
CREATE INDEX bigfoot_sightings_all_county_idx ON bigfoot.sightings_all (county);
CREATE INDEX bigfoot_sightings_all_class_idx ON bigfoot.sightings_all (class);
CREATE INDEX bigfoot_sightings_all_season_idx ON bigfoot.sightings_all (season);
ANALYZE bigfoot.sightings_all;

CREATE TABLE bigfoot.sighting_docs USING rvbbit AS
WITH cleaned AS (
    SELECT
        s.*,
        NULLIF(regexp_replace(COALESCE(s.fixed_year, s.year, ''), '[^0-9]', '', 'g'), '')::int AS report_year,
        concat_ws(E'\n',
            NULLIF('Title: ' || NULLIF(btrim(s.title), ''), 'Title: '),
            NULLIF('Class: ' || NULLIF(btrim(s.class), ''), 'Class: '),
            NULLIF('Season: ' || NULLIF(btrim(s.season), ''), 'Season: '),
            NULLIF('State: ' || NULLIF(btrim(s.state), ''), 'State: '),
            NULLIF('County: ' || NULLIF(btrim(s.county), ''), 'County: '),
            NULLIF('Nearest town: ' || NULLIF(btrim(s.nearesttown), ''), 'Nearest town: '),
            NULLIF('Nearest road: ' || NULLIF(btrim(s.nearestroad), ''), 'Nearest road: '),
            NULLIF('Location details: ' || NULLIF(btrim(s.locationdetails), ''), 'Location details: '),
            NULLIF('Observed: ' || NULLIF(btrim(s.observed), ''), 'Observed: '),
            NULLIF('Also noticed: ' || NULLIF(btrim(s.alsonoticed), ''), 'Also noticed: '),
            NULLIF('Other witnesses: ' || NULLIF(btrim(s.otherwitnesses), ''), 'Other witnesses: '),
            NULLIF('Other stories: ' || NULLIF(btrim(s.otherstories), ''), 'Other stories: '),
            NULLIF('Time and conditions: ' || NULLIF(btrim(s.timeandconditions), ''), 'Time and conditions: '),
            NULLIF('Environment: ' || NULLIF(btrim(s.environment), ''), 'Environment: ')
        ) AS report_text
    FROM bigfoot.sightings_all s
    WHERE NULLIF(btrim(s.observed), '') IS NOT NULL
      AND length(s.observed) >= 80
)
SELECT *
FROM cleaned
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
LIMIT :sample_rows;

CREATE INDEX bigfoot_sighting_docs_bfroid_idx ON bigfoot.sighting_docs (bfroid);
CREATE INDEX bigfoot_sighting_docs_state_idx ON bigfoot.sighting_docs (state);
CREATE INDEX bigfoot_sighting_docs_county_idx ON bigfoot.sighting_docs (county);
CREATE INDEX bigfoot_sighting_docs_report_year_idx ON bigfoot.sighting_docs (report_year);
ANALYZE bigfoot.sighting_docs;

SELECT count(*) AS full_csv_rows FROM bigfoot.sightings_all;
SELECT count(*) AS notebook_rows FROM bigfoot.sighting_docs;

SELECT rvbbit.export_to_parquet('bigfoot.sighting_docs'::regclass) AS exported_rows;
