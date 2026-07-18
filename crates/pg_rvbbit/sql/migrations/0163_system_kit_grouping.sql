-- 0163: group the shipped system plates under the 'rvbbit' kit so the shelf
-- shows them as a family (kit metadata included; pure data, idempotent).
SELECT rvbbit.upsert_kit('rvbbit', 'rvbbit', 'The product''s own surfaces', NULL, '4.0.12');
UPDATE rvbbit.plates SET kit = 'rvbbit' WHERE plate_id = 'system/health' AND kit IS DISTINCT FROM 'rvbbit';
