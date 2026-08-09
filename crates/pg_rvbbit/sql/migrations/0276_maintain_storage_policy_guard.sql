-- 0276: Make the legacy storage heartbeat a consumer of accelerator policy,
-- never a creator of accelerator supply.
--
-- maintain_storage() predates accel_policy. Its compaction loop selected any
-- dirty registered table, including a manual table with no row groups. Since
-- compact() can build a first baseline, the hourly housekeeping job could
-- silently re-accelerate tables that an operator deliberately reset. Restrict
-- that loop to already-built tables whose effective policy explicitly permits
-- automatic work.

DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
    changed boolean := false;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.maintain_storage(bigint,boolean)'::regprocedure
    );

    IF position('Storage housekeeping must never create supply' IN definition) = 0 THEN
        needle := $needle$
        SELECT t.table_oid::regclass AS rel
        FROM rvbbit.tables t
        JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE ds.shadow_heap_dirty
        ORDER BY t.created_at
$needle$;

        replacement := $replacement$
        SELECT t.table_oid::regclass AS rel
        FROM rvbbit.tables t
        JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
        JOIN rvbbit.accel_policy_effective policy
          ON policy.table_oid = t.table_oid
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE ds.shadow_heap_dirty
          -- Storage housekeeping must never create supply. Baselines are an
          -- explicit operator/autopilot decision, and manual means no automatic
          -- refresh even when an old hourly storage job remains installed.
          AND policy.active
          AND policy.strategy <> 'manual'
          AND EXISTS (
              SELECT 1
                FROM rvbbit.row_groups rg
               WHERE rg.table_oid = t.table_oid
          )
        ORDER BY t.created_at
$replacement$;

        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION
                '0276 could not locate the maintain_storage compaction candidate query';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('Variant housekeeping respects the same automation boundary' IN definition) = 0 THEN
        needle := $needle$
                    count(rg.*) AS row_groups,
                    count(rgv.*) AS variants,
                    rvbbit.workload_layout_variants_pending(t.table_oid) AS workload_pending
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              AND (variants = 0 OR newest_variant < newest_rg OR workload_pending)
$needle$;

        replacement := $replacement$
                    count(rg.*) AS row_groups,
                    count(rgv.*) AS variants,
                    policy.active AS policy_active,
                    policy.strategy AS policy_strategy,
                    rvbbit.workload_layout_variants_pending(t.table_oid) AS workload_pending
                FROM rvbbit.tables t
                JOIN rvbbit.accel_policy_effective policy
                  ON policy.table_oid = t.table_oid
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid, policy.active, policy.strategy
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              -- Variant housekeeping respects the same automation boundary as
              -- canonical maintenance. Manual tables stay completely manual.
              AND policy_active
              AND policy_strategy <> 'manual'
              AND (variants = 0 OR newest_variant < newest_rg OR workload_pending)
$replacement$;

        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION
                '0276 could not locate the maintain_storage variant candidate query';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF changed THEN
        EXECUTE definition;
    END IF;
END
$migration$;

COMMENT ON FUNCTION rvbbit.maintain_storage(bigint, boolean) IS
    'Storage housekeeping for already-built, auto-managed accelerators plus layout, retention, and orphan cleanup. It never creates a baseline or refreshes a manual table.';
