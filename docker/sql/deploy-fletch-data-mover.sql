SELECT rvbbit.seed_capability_catalog();

SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'data/fletch-data-mover',
  target_selector => '{"capability":true,"docker":true,"gpu":false}'::jsonb,
  install_mode => 'build'
);
