-- Text-surrogate type round-trip verify (the Salesforce-keys case).
--
-- uuid / numeric / inet / interval / enum aren't natively Arrow-representable,
-- so compaction stores them in parquet as their canonical TEXT and the native
-- custom scan reconstructs the real type on read via its input function
-- (custom_scan::ColumnReader::Utf8 / Utf8Recon::TypeInput). This verifies the
-- READ half end-to-end: values, the declared column TYPE, an equality predicate
-- (PG's own uuid operators on the reconstructed datum), numeric precision, enum
-- labels, and uuid ORDER BY — all must match the heap.
--
-- Must run against a LIVE instance (parquet authoritative only after a committed
-- compaction); the single-transaction pg_test harness can't publish row groups.
-- The EXPORT half is covered by the pg_test export_to_parquet_supports_uuid_numeric_enum.
--   docker exec -i rvbbit-pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 \
--     < docker/sql/uuid-surrogate-verify.sql
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS uuidsurr_verify;
DROP TYPE IF EXISTS uuidsurr_status;
CREATE TYPE uuidsurr_status AS ENUM ('open', 'closed', 'pending');
CREATE TABLE uuidsurr_verify (
    id     uuid,
    amount numeric,
    ip     inet,
    dur    interval,
    status uuidsurr_status,
    note   text
) USING rvbbit;
INSERT INTO uuidsurr_verify VALUES
    ('11111111-1111-1111-1111-111111111111', 12.34,          '10.0.0.1',       interval '1 day 2 hours', 'open',    'a'),
    ('22222222-2222-2222-2222-222222222222', 9999999999.99,  '2001:db8::1',    interval '90 minutes',    'closed',  'b'),
    ('33333333-3333-3333-3333-333333333333', -0.5,           '192.168.1.0/24', interval '0',             'pending', 'c');

-- Compact to parquet (keep_heap=false → parquet authoritative) and force the
-- native custom scan so the reconstruction path is exercised.
SELECT rvbbit.refresh_acceleration('uuidsurr_verify'::regclass, false);
SET rvbbit.route_force_candidate = 'rvbbit_native';

DO $$
DECLARE
    v_type text; v_id uuid; v_amount numeric; v_ip inet; v_dur interval; v_status text; v_order text; n bigint;
BEGIN
    -- the column reads back as its declared type, not text (OFFSET 0 defeats the
    -- count/metadata shortcut so the scan actually emits + reconstructs rows)
    SELECT pg_typeof(id)::text INTO v_type FROM (SELECT id FROM uuidsurr_verify OFFSET 0) s LIMIT 1;
    IF v_type <> 'uuid' THEN RAISE EXCEPTION 'FAIL: id reads back as % not uuid', v_type; END IF;

    -- exact value round-trip for each surrogate type (row 'a')
    SELECT id, amount, ip, dur INTO v_id, v_amount, v_ip, v_dur
      FROM (SELECT * FROM uuidsurr_verify WHERE note = 'a' OFFSET 0) s;
    IF v_id <> '11111111-1111-1111-1111-111111111111'::uuid THEN RAISE EXCEPTION 'FAIL uuid value: %', v_id; END IF;
    IF v_amount <> 12.34 THEN RAISE EXCEPTION 'FAIL numeric value: %', v_amount; END IF;
    IF v_ip <> '10.0.0.1'::inet THEN RAISE EXCEPTION 'FAIL inet value: %', v_ip; END IF;
    IF v_dur <> interval '1 day 2 hours' THEN RAISE EXCEPTION 'FAIL interval value: %', v_dur; END IF;

    -- uuid equality predicate evaluated on the reconstructed datum (PG uuid ops)
    SELECT count(*) INTO n
      FROM (SELECT 1 FROM uuidsurr_verify WHERE id = '22222222-2222-2222-2222-222222222222'::uuid OFFSET 0) s;
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL: uuid equality predicate matched % rows, expected 1', n; END IF;

    -- numeric precision preserved for a large exact value
    SELECT amount INTO v_amount FROM (SELECT amount FROM uuidsurr_verify WHERE note = 'b' OFFSET 0) s;
    IF v_amount <> 9999999999.99 THEN RAISE EXCEPTION 'FAIL numeric precision: %', v_amount; END IF;

    -- enum reconstructs to the right label
    SELECT status::text INTO v_status FROM (SELECT status FROM uuidsurr_verify WHERE note = 'c' OFFSET 0) s;
    IF v_status <> 'pending' THEN RAISE EXCEPTION 'FAIL enum value: %', v_status; END IF;

    -- ORDER BY uuid: canonical lowercase text sorts in uuid byte order, so 1<2<3
    SELECT string_agg(note, '' ORDER BY id) INTO v_order FROM (SELECT id, note FROM uuidsurr_verify OFFSET 0) s;
    IF v_order <> 'abc' THEN RAISE EXCEPTION 'FAIL uuid ordering: got %', v_order; END IF;

    RAISE NOTICE 'uuid-surrogate verify: PASS (uuid/numeric/inet/interval/enum round-trip + predicate + order)';
END $$;

RESET rvbbit.route_force_candidate;
DROP TABLE uuidsurr_verify;
DROP TYPE uuidsurr_status;
