\echo == Warren deployment inventory ==
SELECT node_name, node_status, deployment_name, deployment_status, backend_name, operator_name
FROM rvbbit.warren_inventory
WHERE node_name = 'e2e-warren-local'
ORDER BY deployment_updated_at DESC NULLS LAST
LIMIT 5;

\echo == Warren backend probe ==
SELECT jsonb_pretty(rvbbit.backend_probe('warren_smoke_echo'));

\echo == Warren operator call ==
DO $$
DECLARE
    got text;
BEGIN
    SELECT rvbbit.warren_smoke_echo('warren smoke test')->>'echo'
    INTO got;

    IF got IS DISTINCT FROM 'warren smoke test' THEN
        RAISE EXCEPTION 'warren_smoke_echo returned %, expected %',
            got, 'warren smoke test';
    END IF;
END $$;

SELECT rvbbit.warren_smoke_echo('warren smoke test')->>'echo' AS echo;
