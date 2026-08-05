-- 0244: Quiet, derived connective tissue for published artifacts and private notebooks.
--
-- Artifact Areas are constrained to a shared vocabulary, link edges are
-- deterministically extracted from immutable artifact versions, and session
-- synopses remain owner-scoped through their parent Calliope session.

CREATE TABLE IF NOT EXISTS rvbbit.artifact_areas (
    id text PRIMARY KEY,
    label text UNIQUE NOT NULL,
    description text NOT NULL DEFAULT '',
    keywords text[] NOT NULL DEFAULT '{}'::text[],
    sort_order integer NOT NULL DEFAULT 100,
    source text NOT NULL DEFAULT 'system',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO rvbbit.artifact_areas (id,label,description,keywords,sort_order) VALUES
 ('executive','Executive','Leadership, strategy, company health, and board-level planning',ARRAY['executive','leadership','strategy','board','company plan','forecast'],10),
 ('revenue','Revenue','Sales, pipeline, bookings, accounts, and commercial performance',ARRAY['revenue','sales','pipeline','booking','deal','lead','account','conversion'],20),
 ('marketing','Marketing','Campaigns, acquisition, attribution, audience, and brand',ARRAY['marketing','campaign','attribution','acquisition','web traffic','audience','brand','channel'],30),
 ('customer','Customer','Customer success, service, support, retention, and experience',ARRAY['customer','support','case','ticket','retention','churn','service','success'],40),
 ('product','Product','Product usage, roadmap, feature, quality, and delivery',ARRAY['product','feature','roadmap','usage','release','sprint','bug','quality'],50),
 ('operations','Operations','Operational delivery, process, capacity, inventory, and logistics',ARRAY['operation','workflow','process','capacity','inventory','logistics','fulfillment','sla'],60),
 ('finance','Finance','Financial planning, accounting, cash, cost, margin, and budget',ARRAY['finance','financial','budget','cost','expense','margin','cash','invoice'],70),
 ('people','People','Hiring, workforce, talent, compensation, and organizational health',ARRAY['people','employee','workforce','hiring','talent','compensation','headcount','hr'],80),
 ('technology','Technology','Engineering, data, infrastructure, security, and reliability',ARRAY['technology','engineering','database','infrastructure','security','reliability','etl','pipeline failure'],90),
 ('risk','Risk','Compliance, controls, audit, legal, fraud, and business risk',ARRAY['risk','compliance','audit','legal','fraud','control','privacy','governance'],100),
 ('other','Other','Cross-functional or not yet confidently classified',ARRAY[]::text[],999)
ON CONFLICT DO NOTHING;

ALTER TABLE rvbbit.dashboards
    ADD COLUMN IF NOT EXISTS area_id text,
    ADD COLUMN IF NOT EXISTS area_source text,
    ADD COLUMN IF NOT EXISTS area_confidence real,
    ADD COLUMN IF NOT EXISTS area_updated_at timestamptz;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='dashboards_area_id_fkey'
          AND conrelid='rvbbit.dashboards'::regclass
    ) THEN
        ALTER TABLE rvbbit.dashboards
            ADD CONSTRAINT dashboards_area_id_fkey
            FOREIGN KEY (area_id) REFERENCES rvbbit.artifact_areas(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='dashboards_area_source_check'
          AND conrelid='rvbbit.dashboards'::regclass
    ) THEN
        ALTER TABLE rvbbit.dashboards
            ADD CONSTRAINT dashboards_area_source_check
            CHECK (area_source IS NULL OR area_source IN ('auto','manual'));
    END IF;
END
$do$;

CREATE TABLE IF NOT EXISTS rvbbit.artifact_catalog_enrichments (
    dashboard_id bigint NOT NULL,
    version integer NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    input_hash text NOT NULL,
    area_id text REFERENCES rvbbit.artifact_areas(id) ON DELETE SET NULL,
    confidence real,
    rationale text,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    model text,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    not_before timestamptz NOT NULL DEFAULT now(),
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dashboard_id,version),
    FOREIGN KEY (dashboard_id,version)
        REFERENCES rvbbit.dashboard_versions(dashboard_id,version) ON DELETE CASCADE,
    CONSTRAINT artifact_catalog_enrichments_status_check
        CHECK (status IN ('pending','running','ready','failed','disabled'))
);
CREATE INDEX IF NOT EXISTS artifact_catalog_enrichments_queue_idx
    ON rvbbit.artifact_catalog_enrichments (status,not_before,enqueued_at);

ALTER TABLE rvbbit.dashboard_deps
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE VIEW rvbbit.live_apps AS
  SELECT d.id, d.slug, d.name, d.description, d.owner_email, d.team, d.status,
         d.runtime_kind, d.app_kind, d.latest_version, d.manifest, d.last_health,
         d.last_debug_at, d.created_at, d.updated_at,
         coalesce(dep.queries, 0)::int AS queries,
         coalesce(dep.tables, 0)::int AS tables,
         coalesce(dep.metrics, 0)::int AS metrics,
         coalesce(dep.semantic_objects, 0)::int AS semantic_objects,
         d.area_id,area.label AS area_label,d.area_source,d.area_confidence,d.area_updated_at
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

CREATE TABLE IF NOT EXISTS rvbbit.calliope_session_synopses (
    session_id uuid PRIMARY KEY REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending',
    input_hash text,
    synopsis text,
    through_ordinal integer NOT NULL DEFAULT 0,
    provider text,
    model text,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    not_before timestamptz NOT NULL DEFAULT now(),
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_session_synopses_status_check
        CHECK (status IN ('pending','running','ready','failed','disabled'))
);
CREATE INDEX IF NOT EXISTS calliope_session_synopses_queue_idx
    ON rvbbit.calliope_session_synopses (status,not_before,enqueued_at);

COMMENT ON TABLE rvbbit.artifact_areas IS
    'Controlled organizational vocabulary for published artifacts; automatic classification may only choose active rows.';
COMMENT ON COLUMN rvbbit.dashboard_deps.metadata IS
    'Structured derived edge detail, including target version and link text for artifact navigation links.';
COMMENT ON TABLE rvbbit.calliope_session_synopses IS
    'Debounced derived synopsis for one private Calliope notebook; visibility inherits the owner-scoped parent session.';
