-- Queue the built-in MCP Gateway runtime as a Warren capability.
--
-- This is intentionally catalog-driven: Warren claims the job, materializes
-- one isolated deployment project, builds/runs the runtime, probes it,
-- and registers mcp_default in rvbbit.mcp_gateways.

SELECT rvbbit.seed_capability_catalog();

SELECT rvbbit.deploy_catalog_capability(
    catalog_id => 'runtimes/mcp-gateway',
    target_selector => '{"capability":true,"docker":true,"gpu":false}'::jsonb,
    job_name => 'mcp_gateway_runtime'
) AS job_id;
