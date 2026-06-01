-- Queue the built-in managed CPython runtime as a Warren capability.
--
-- This is intentionally catalog-driven: Warren claims the job, materializes
-- one isolated deployment project, builds/runs the runtime,
-- probes it, and registers python_default.

SELECT rvbbit.seed_capability_catalog();

SELECT rvbbit.deploy_catalog_capability(
    catalog_id => 'runtimes/python-runtime',
    target_selector => '{"capability":true,"docker":true,"gpu":false}'::jsonb,
    job_name => 'python_runtime'
) AS job_id;
