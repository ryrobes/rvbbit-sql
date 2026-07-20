-- 0196: register the Google Drive connector backend by default. The brain's
-- remote-source orchestrator (0046/0047) resolves a source's connector via
-- rvbbit.backends — sources default to config->>'connector' = 'gdrive_connector',
-- but nothing ever registered that row, so "add a Drive source" dead-ended
-- with "no connector endpoint" until an operator hand-inserted it (the
-- AWS-box situation this migration retires). The endpoint is the compose
-- service name — the connector ships in the release/uber compose behind the
-- "gdrive" profile; the row existing while the container is absent is fine
-- (sync fails with a clear connection error, and the brain UI shows state).
--
-- Auth note: the connector reads GDRIVE_SA_KEY from ITS OWN environment —
-- credentials never transit rvbbit. auth_header_env here is the OPTIONAL
-- bearer token rvbbit sends to the connector (CONNECTOR_TOKEN on the
-- sidecar), for deployments that expose the connector beyond the trusted
-- network.

DO $do$
BEGIN
    PERFORM rvbbit.register_backend(
        backend_name      => 'gdrive_connector',
        backend_endpoint  => coalesce(nullif(current_setting('rvbbit.gdrive_connector_endpoint', true), ''),
                                      'http://rvbbit-gdrive-connector:8080/sync'),
        backend_transport => 'rvbbit',
        backend_batch_size=> 1,
        backend_max_concur=> 1,
        backend_timeout_ms=> 900000,
        backend_auth_env  => 'GDRIVE_CONNECTOR_TOKEN',
        backend_description => 'Google Drive → brain remote-source connector (lists folders, maps sharing to ACLs, stages changed files). Ships in compose behind the "gdrive" profile; set GDRIVE_SA_KEY on the sidecar.');
END $do$;
