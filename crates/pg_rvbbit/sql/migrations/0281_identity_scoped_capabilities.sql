-- 0281: identity-scoped capability discovery.
--
-- The capability graph historically described installation-wide abilities and
-- therefore had no audience model. Preserve that contract: a capability with
-- no policy row remains visible to every legacy caller. Only an explicitly
-- governed capability is identity-scoped. This lets installations introduce
-- private Playbooks and restricted MCP abilities without backfilling hundreds
-- of benign existing entries or changing today's search results.
--
-- Warehouse resolves the trusted application subject. It passes that value to
-- capability_search_for() internally; it is never accepted from an MCP tool
-- argument or inferred from a telemetry GUC.

CREATE TABLE IF NOT EXISTS rvbbit.capability_access_policies (
    capability_kind text NOT NULL,
    capability_name text NOT NULL,
    visibility text NOT NULL DEFAULT 'restricted',
    owner_email text,
    revision bigint NOT NULL DEFAULT 1,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (capability_kind,capability_name),
    CONSTRAINT capability_access_policies_kind_check
        CHECK (length(btrim(capability_kind)) BETWEEN 1 AND 80),
    CONSTRAINT capability_access_policies_name_check
        CHECK (length(btrim(capability_name)) BETWEEN 1 AND 500),
    CONSTRAINT capability_access_policies_visibility_check
        CHECK (visibility IN ('everyone','restricted')),
    CONSTRAINT capability_access_policies_owner_check CHECK (
        owner_email IS NULL OR
        (owner_email=lower(btrim(owner_email)) AND owner_email LIKE '%@%')
    ),
    CONSTRAINT capability_access_policies_revision_check CHECK (revision >= 1)
);
CREATE INDEX IF NOT EXISTS capability_access_policies_owner_idx
    ON rvbbit.capability_access_policies (owner_email)
    WHERE owner_email IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.capability_access_grants (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    capability_kind text NOT NULL,
    capability_name text NOT NULL,
    team_id uuid REFERENCES rvbbit.teams(id) ON DELETE CASCADE,
    principal_email text REFERENCES rvbbit.application_principals(email) ON DELETE CASCADE,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT capability_access_grants_policy_fkey
        FOREIGN KEY (capability_kind,capability_name)
        REFERENCES rvbbit.capability_access_policies(capability_kind,capability_name)
        ON DELETE CASCADE,
    CONSTRAINT capability_access_grants_one_grantee_check CHECK (
        num_nonnulls(team_id,principal_email)=1
    ),
    CONSTRAINT capability_access_grants_email_check CHECK (
        principal_email IS NULL OR
        (principal_email=lower(btrim(principal_email)) AND principal_email LIKE '%@%')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS capability_access_grants_team_key
    ON rvbbit.capability_access_grants
        (capability_kind,capability_name,team_id)
    WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS capability_access_grants_person_key
    ON rvbbit.capability_access_grants
        (capability_kind,capability_name,principal_email)
    WHERE principal_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS capability_access_grants_team_lookup_idx
    ON rvbbit.capability_access_grants (team_id,capability_kind,capability_name)
    WHERE team_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS capability_access_grants_person_lookup_idx
    ON rvbbit.capability_access_grants
        (principal_email,capability_kind,capability_name)
    WHERE principal_email IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.capability_access_events (
    event_id uuid PRIMARY KEY,
    capability_kind text NOT NULL,
    capability_name text NOT NULL,
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
    CONSTRAINT capability_access_events_payload_check CHECK (
        jsonb_typeof(before_state)='object'
        AND jsonb_typeof(after_state)='object'
        AND jsonb_typeof(detail)='object'
    )
);
CREATE INDEX IF NOT EXISTS capability_access_events_capability_created_idx
    ON rvbbit.capability_access_events
        (capability_kind,capability_name,created_at DESC);
CREATE INDEX IF NOT EXISTS capability_access_events_subject_created_idx
    ON rvbbit.capability_access_events (human_subject,created_at DESC);

CREATE OR REPLACE FUNCTION rvbbit._capability_access_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'Capability access events are append-only';
END
$fn$;
DROP TRIGGER IF EXISTS capability_access_events_append_only
    ON rvbbit.capability_access_events;
CREATE TRIGGER capability_access_events_append_only
BEFORE UPDATE OR DELETE ON rvbbit.capability_access_events
FOR EACH ROW EXECUTE FUNCTION rvbbit._capability_access_events_append_only();

CREATE OR REPLACE FUNCTION rvbbit.capability_can_use(
    p_kind text,
    p_name text,
    p_subject text
) RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
    SELECT CASE
        -- Compatibility rule: absence of policy means the installation-wide
        -- capability that existed before application ACLs. Even legacy service
        -- callers retain discovery until an administrator explicitly governs it.
        WHEN NOT EXISTS (
            SELECT 1
              FROM rvbbit.capability_access_policies p
             WHERE p.capability_kind=p_kind
               AND p.capability_name=p_name
        ) THEN true
        ELSE EXISTS (
            SELECT 1
              FROM rvbbit.capability_access_policies p
             WHERE p.capability_kind=p_kind
               AND p.capability_name=p_name
               AND nullif(lower(btrim(p_subject)),'') LIKE '%@%'
               AND (
                    p.visibility='everyone'
                    OR lower(p.owner_email)=lower(btrim(p_subject))
                    OR EXISTS (
                        SELECT 1
                          FROM rvbbit.capability_access_grants g
                         WHERE g.capability_kind=p.capability_kind
                           AND g.capability_name=p.capability_name
                           AND g.principal_email=lower(btrim(p_subject))
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM rvbbit.capability_access_grants g
                          JOIN rvbbit.teams t
                            ON t.id=g.team_id AND NOT t.archived
                         WHERE g.capability_kind=p.capability_kind
                           AND g.capability_name=p.capability_name
                           AND (
                                t.system_key='everyone'
                                OR EXISTS (
                                    SELECT 1
                                      FROM rvbbit.team_members m
                                     WHERE m.team_id=t.id
                                       AND m.principal_email=lower(btrim(p_subject))
                                )
                           )
                    )
               )
        )
    END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.capability_search_for(
    p_subject text,
    p_query text,
    p_k int DEFAULT 8,
    p_kinds text[] DEFAULT NULL
) RETURNS TABLE (kind text, name text, score float8, doc text)
LANGUAGE sql STABLE
AS $fn$
    WITH bounds AS MATERIALIZED (
        SELECT greatest(1,least(coalesce(p_k,8),100))::int AS wanted
    ),
    ranked AS MATERIALIZED (
        -- Access is applied after semantic ranking, so over-fetch enough to
        -- replace hidden hits without changing data_search's global index.
        SELECT ds.kind,ds.rel_name AS name,ds.match_score AS score,
               ds.doc,ds.source_rank
          FROM bounds b
          CROSS JOIN LATERAL rvbbit.data_search(
              p_query,
              least(greatest(b.wanted * 8,64),512),
              p_kinds,
              'rvbbit_capabilities'
          ) WITH ORDINALITY AS ds(
              node_id,kind,schema_name,rel_name,col_name,match_score,doc,source_rank
          )
    ), visible AS MATERIALIZED (
        SELECT r.kind,r.name,r.score AS raw_score,r.doc,r.source_rank
          FROM ranked r
         WHERE rvbbit.capability_can_use(r.kind,r.name,p_subject)
    ), normalized AS (
        -- data_search normalizes against its global top hit. Re-normalize only
        -- after ACL filtering so a depressed top score cannot hint that a
        -- stronger hidden result existed.
        SELECT v.kind,v.name,
               (v.raw_score / NULLIF(max(v.raw_score) OVER (),0))::float8 AS score,
               v.doc,v.source_rank
          FROM visible v
    )
    SELECT n.kind,n.name,n.score,n.doc
      FROM normalized n
     -- Preserve data_search's existing relative order, including equal-score
     -- ties. Filtering may remove entries but must not reshuffle legacy ones.
     ORDER BY n.source_rank
     LIMIT (SELECT wanted FROM bounds);
$fn$;

-- Raw SQL has no trusted application subject. Keep its historical results for
-- all ungoverned entries, but fail closed for anything explicitly governed.
CREATE OR REPLACE FUNCTION rvbbit.capability_search(
    q text,
    k int DEFAULT 8,
    kinds text[] DEFAULT NULL
) RETURNS TABLE (kind text, name text, score float8, doc text)
LANGUAGE sql STABLE
AS $fn$
    SELECT * FROM rvbbit.capability_search_for(NULL,q,k,kinds);
$fn$;

COMMENT ON TABLE rvbbit.capability_access_policies IS
    'Optional application ACLs for capability identities. No row means legacy installation-wide visibility; explicit rows are Everyone or restricted.';
COMMENT ON TABLE rvbbit.capability_access_grants IS
    'Exact person or flat-Team grants for explicitly governed capabilities.';
COMMENT ON TABLE rvbbit.capability_access_events IS
    'Append-only receipts reserved for audited capability audience changes.';
COMMENT ON FUNCTION rvbbit.capability_can_use(text,text,text) IS
    'Evaluate capability discovery for an already-verified application subject; ungoverned capabilities retain legacy visibility.';
COMMENT ON FUNCTION rvbbit.capability_search_for(text,text,int,text[]) IS
    'Identity-filtered capability discovery. Warehouse supplies p_subject from its trusted request context, never an MCP argument.';
