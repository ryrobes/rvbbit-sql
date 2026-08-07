-- 0254_artifact_access — owner-managed artifact sharing and reversible archive.
--
-- Existing artifacts are granted to the dynamic Everyone Team exactly once.
-- The singleton marker is intentionally inserted before any future publish, so
-- artifacts created after this migration remain owner-private by default.

ALTER TABLE rvbbit.dashboards
    ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS archived_at timestamptz,
    ADD COLUMN IF NOT EXISTS archived_by text,
    ADD COLUMN IF NOT EXISTS access_revision bigint NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS dashboards_active_updated_idx
    ON rvbbit.dashboards (updated_at DESC) WHERE NOT archived;
CREATE INDEX IF NOT EXISTS dashboards_owner_archived_idx
    ON rvbbit.dashboards (lower(owner_email),archived,updated_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.artifact_view_grants (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artifact_id bigint NOT NULL REFERENCES rvbbit.dashboards(id) ON DELETE CASCADE,
    team_id uuid REFERENCES rvbbit.teams(id) ON DELETE CASCADE,
    principal_email text REFERENCES rvbbit.application_principals(email) ON DELETE CASCADE,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT artifact_view_grants_one_grantee_check CHECK (
        num_nonnulls(team_id,principal_email)=1
    ),
    CONSTRAINT artifact_view_grants_email_normalized_check CHECK (
        principal_email IS NULL OR
        (principal_email=lower(btrim(principal_email)) AND principal_email LIKE '%@%')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS artifact_view_grants_team_key
    ON rvbbit.artifact_view_grants (artifact_id,team_id) WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS artifact_view_grants_person_key
    ON rvbbit.artifact_view_grants (artifact_id,principal_email)
    WHERE principal_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS artifact_view_grants_team_lookup_idx
    ON rvbbit.artifact_view_grants (team_id,artifact_id) WHERE team_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS artifact_view_grants_person_lookup_idx
    ON rvbbit.artifact_view_grants (principal_email,artifact_id)
    WHERE principal_email IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.artifact_access_events (
    event_id uuid PRIMARY KEY,
    artifact_id bigint,
    artifact_slug text NOT NULL,
    event_type text NOT NULL,
    credential_actor text,
    human_subject text NOT NULL,
    auth_mode text NOT NULL,
    delegated boolean NOT NULL DEFAULT false,
    platform text,
    session_ref text,
    before_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT artifact_access_events_payload_check CHECK (
        jsonb_typeof(before_state)='object'
        AND jsonb_typeof(after_state)='object'
        AND jsonb_typeof(detail)='object'
    )
);
-- Audit receipts outlive their source row even if a database administrator
-- performs an out-of-band hard delete. Application surfaces only archive.
ALTER TABLE rvbbit.artifact_access_events
    DROP CONSTRAINT IF EXISTS artifact_access_events_artifact_id_fkey;
CREATE INDEX IF NOT EXISTS artifact_access_events_artifact_created_idx
    ON rvbbit.artifact_access_events (artifact_id,created_at DESC);
CREATE INDEX IF NOT EXISTS artifact_access_events_subject_created_idx
    ON rvbbit.artifact_access_events (human_subject,created_at DESC);

CREATE OR REPLACE FUNCTION rvbbit._artifact_access_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'Artifact access events are append-only';
END
$fn$;
DROP TRIGGER IF EXISTS artifact_access_events_append_only ON rvbbit.artifact_access_events;
CREATE TRIGGER artifact_access_events_append_only
BEFORE UPDATE OR DELETE ON rvbbit.artifact_access_events
FOR EACH ROW EXECUTE FUNCTION rvbbit._artifact_access_events_append_only();

CREATE TABLE IF NOT EXISTS rvbbit.artifact_access_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    grandfathered_at timestamptz NOT NULL DEFAULT now(),
    grandfathered_count integer NOT NULL DEFAULT 0
);

DO $do$
DECLARE
    first_install boolean := false;
    granted_count integer := 0;
BEGIN
    INSERT INTO rvbbit.artifact_access_state (singleton)
    VALUES (true)
    ON CONFLICT (singleton) DO NOTHING
    RETURNING true INTO first_install;

    IF coalesce(first_install,false) THEN
        INSERT INTO rvbbit.artifact_view_grants
            (artifact_id,team_id,granted_by)
        SELECT d.id,t.id,'system:0254-grandfather'
        FROM rvbbit.dashboards d
        CROSS JOIN rvbbit.teams t
        WHERE t.system_key='everyone'
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS granted_count = ROW_COUNT;
        UPDATE rvbbit.artifact_access_state
        SET grandfathered_count=granted_count
        WHERE singleton;
    END IF;
END
$do$;

CREATE OR REPLACE FUNCTION rvbbit.artifact_can_view(
    p_artifact_id bigint,
    p_subject text,
    p_include_archived boolean DEFAULT false
) RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
        FROM rvbbit.dashboards d
        WHERE d.id=p_artifact_id
          AND p_subject IS NOT NULL
          AND btrim(p_subject) LIKE '%@%'
          AND (
              NOT d.archived
              OR (
                  p_include_archived
                  AND lower(d.owner_email)=lower(btrim(p_subject))
              )
          )
          AND (
              lower(d.owner_email)=lower(btrim(p_subject))
              OR EXISTS (
                  SELECT 1 FROM rvbbit.artifact_view_grants g
                  WHERE g.artifact_id=d.id
                    AND g.principal_email=lower(btrim(p_subject))
              )
              OR EXISTS (
                  SELECT 1
                  FROM rvbbit.artifact_view_grants g
                  JOIN rvbbit.teams t ON t.id=g.team_id AND NOT t.archived
                  WHERE g.artifact_id=d.id
                    AND (
                        t.system_key='everyone'
                        OR EXISTS (
                            SELECT 1 FROM rvbbit.team_members m
                            WHERE m.team_id=t.id
                              AND m.principal_email=lower(btrim(p_subject))
                        )
                    )
              )
          )
    )
$fn$;

COMMENT ON FUNCTION rvbbit.artifact_can_view(bigint,text,boolean) IS
    'Application-layer artifact visibility for an already-verified human subject.';

-- Append lifecycle fields so CREATE OR REPLACE remains compatible with the
-- preceding view shape and downstream readers can recover owner archives.
CREATE OR REPLACE VIEW rvbbit.live_apps AS
  SELECT d.id, d.slug, d.name, d.description, d.owner_email, d.team, d.status,
         d.runtime_kind, d.app_kind, d.latest_version, d.manifest, d.last_health,
         d.last_debug_at, d.created_at, d.updated_at,
         coalesce(dep.queries, 0)::int AS queries,
         coalesce(dep.tables, 0)::int AS tables,
         coalesce(dep.metrics, 0)::int AS metrics,
         coalesce(dep.semantic_objects, 0)::int AS semantic_objects,
         d.area_id,area.label AS area_label,d.area_source,d.area_confidence,d.area_updated_at,
         d.archived,d.archived_at,d.archived_by,d.access_revision
  FROM rvbbit.dashboards d
  LEFT JOIN rvbbit.artifact_areas area ON area.id=d.area_id
  LEFT JOIN LATERAL (
    SELECT
           count(*) FILTER (WHERE kind = 'query') AS queries,
           count(*) FILTER (WHERE kind = 'table') AS tables,
           count(*) FILTER (WHERE kind = 'metric') AS metrics,
           count(*) FILTER (WHERE kind = 'semantic') AS semantic_objects
    FROM rvbbit.dashboard_deps
    WHERE dashboard_id = d.id AND version = d.latest_version
  ) dep ON true;
