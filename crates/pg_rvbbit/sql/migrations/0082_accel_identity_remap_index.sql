-- Speed rebuild tombstone remapping: rebuild joins delete_log to the old
-- identity map by physical row position before finding the staged logical key.
CREATE INDEX IF NOT EXISTS row_identity_map_row_lookup_idx
    ON rvbbit.row_identity_map (table_oid, rg_id, ordinal)
    INCLUDE (key_json, generation);
