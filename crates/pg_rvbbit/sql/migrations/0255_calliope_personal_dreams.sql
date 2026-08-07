-- 0255: Scoped, bounded Dream portfolios and private personal working context.
--
-- Existing Dreams become company Dreams. Personal cycles, observations, probes,
-- and Dreams carry one normalized owner boundary. A user's working context is a
-- private derived cache with explicit guidance and deletion controls; Admins do
-- not gain access to another person's context by virtue of Team membership.

ALTER TABLE rvbbit.calliope_dream_cycles
    ADD COLUMN IF NOT EXISTS scope_kind text NOT NULL DEFAULT 'company',
    ADD COLUMN IF NOT EXISTS owner_email text,
    ADD COLUMN IF NOT EXISTS input_hash text;

UPDATE rvbbit.calliope_dream_cycles
   SET scope_kind='company',owner_email=NULL
 WHERE scope_kind IS DISTINCT FROM 'company' AND owner_email IS NULL;

DROP INDEX IF EXISTS rvbbit.calliope_dream_cycles_nightly_date_idx;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dream_cycles_scope_check'
           AND conrelid='rvbbit.calliope_dream_cycles'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dream_cycles
            ADD CONSTRAINT calliope_dream_cycles_scope_check CHECK (
                (scope_kind='company' AND owner_email IS NULL)
                OR
                (scope_kind='personal' AND owner_email IS NOT NULL
                 AND owner_email=lower(btrim(owner_email)) AND owner_email LIKE '%@%')
            );
    END IF;
END
$do$;

CREATE UNIQUE INDEX IF NOT EXISTS calliope_dream_cycles_nightly_scope_idx
    ON rvbbit.calliope_dream_cycles
       (cycle_date,scope_kind,coalesce(owner_email,''))
    WHERE cycle_kind='nightly';
CREATE INDEX IF NOT EXISTS calliope_dream_cycles_scope_started_idx
    ON rvbbit.calliope_dream_cycles
       (scope_kind,owner_email,started_at DESC);

ALTER TABLE rvbbit.calliope_dreams
    ADD COLUMN IF NOT EXISTS scope_kind text NOT NULL DEFAULT 'company',
    ADD COLUMN IF NOT EXISTS owner_email text,
    ADD COLUMN IF NOT EXISTS problem_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS semantic_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS relevance_kind text NOT NULL DEFAULT 'leverage',
    ADD COLUMN IF NOT EXISTS portfolio_score numeric(5,4) NOT NULL DEFAULT 0.5,
    ADD COLUMN IF NOT EXISTS retired_reason text,
    ADD COLUMN IF NOT EXISTS retired_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_ranked_at timestamptz;

UPDATE rvbbit.calliope_dreams
   SET scope_kind='company',owner_email=NULL
 WHERE scope_kind IS DISTINCT FROM 'company' AND owner_email IS NULL;
UPDATE rvbbit.calliope_dreams
   SET problem_key=left(coalesce(nullif(problem_key,''),title),500),
       semantic_key=left(coalesce(nullif(semantic_key,''),
           regexp_replace(lower(title),'[^a-z0-9]+',' ','g')),1000)
 WHERE problem_key='' OR semantic_key='';
UPDATE rvbbit.calliope_dreams SET portfolio_score=rank_score
 WHERE portfolio_score=0.5 AND rank_score<>0.5;

ALTER TABLE rvbbit.calliope_dreams
    DROP CONSTRAINT IF EXISTS calliope_dreams_fingerprint_key;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_scope_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_scope_check CHECK (
                (scope_kind='company' AND owner_email IS NULL)
                OR
                (scope_kind='personal' AND owner_email IS NOT NULL
                 AND owner_email=lower(btrim(owner_email)) AND owner_email LIKE '%@%')
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_relevance_kind_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_relevance_kind_check
            CHECK (relevance_kind IN (
                'active_work','follow_up','leverage','learning','system_meta'
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_portfolio_score_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_portfolio_score_check
            CHECK (portfolio_score BETWEEN 0 AND 1);
    END IF;
END
$do$;

CREATE UNIQUE INDEX IF NOT EXISTS calliope_dreams_scope_fingerprint_key
    ON rvbbit.calliope_dreams
       (scope_kind,coalesce(owner_email,''),fingerprint);
CREATE INDEX IF NOT EXISTS calliope_dreams_scope_portfolio_idx
    ON rvbbit.calliope_dreams
       (scope_kind,owner_email,portfolio_state,status,
        portfolio_rank NULLS LAST,portfolio_score DESC,updated_at DESC);

-- Normalize the inherited company portfolio immediately. Engaged/adopted work
-- remains promoted; proposed Dreams compete for three promoted and thirty
-- backlog positions. The retired tail stays as durable negative/dedupe memory.
WITH ranked AS (
    SELECT id,row_number() OVER (
        ORDER BY portfolio_score DESC,updated_at DESC,id
    ) AS ordinal
    FROM rvbbit.calliope_dreams
    WHERE scope_kind='company' AND status='proposed'
), normalized AS (
    UPDATE rvbbit.calliope_dreams d
       SET portfolio_state=CASE WHEN r.ordinal<=3 THEN 'promoted' ELSE 'backlog' END,
           portfolio_rank=CASE WHEN r.ordinal<=3 THEN r.ordinal::smallint ELSE NULL END,
           last_ranked_at=now()
      FROM ranked r
     WHERE d.id=r.id
    RETURNING d.id,d.portfolio_state,d.portfolio_score,d.updated_at
), backlog AS (
    SELECT id,row_number() OVER (
        ORDER BY portfolio_score DESC,updated_at DESC,id
    ) AS ordinal
    FROM normalized
    WHERE portfolio_state='backlog'
)
UPDATE rvbbit.calliope_dreams d
   SET status='retired',retired_reason='portfolio_cap',retired_at=now(),
       portfolio_rank=NULL
  FROM backlog b
 WHERE d.id=b.id AND b.ordinal>30;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_user_dossiers (
    owner_email text PRIMARY KEY,
    version integer NOT NULL DEFAULT 0,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_receipts jsonb NOT NULL DEFAULT '[]'::jsonb,
    user_guidance text NOT NULL DEFAULT '',
    paused boolean NOT NULL DEFAULT false,
    input_hash text,
    source_watermark timestamptz,
    evidence_count integer NOT NULL DEFAULT 0,
    provider text,
    model text,
    last_error text,
    generated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_user_dossiers_owner_check
        CHECK (owner_email=lower(btrim(owner_email)) AND owner_email LIKE '%@%'),
    CONSTRAINT calliope_user_dossiers_version_check CHECK (version>=0),
    CONSTRAINT calliope_user_dossiers_count_check CHECK (evidence_count>=0),
    CONSTRAINT calliope_user_dossiers_json_check CHECK (
        jsonb_typeof(context)='object' AND jsonb_typeof(evidence_receipts)='array'
    )
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_user_dossier_versions (
    owner_email text NOT NULL REFERENCES rvbbit.calliope_user_dossiers(owner_email)
        ON DELETE CASCADE,
    version integer NOT NULL,
    context jsonb NOT NULL,
    evidence_receipts jsonb NOT NULL DEFAULT '[]'::jsonb,
    input_hash text NOT NULL,
    provider text,
    model text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_email,version),
    CONSTRAINT calliope_user_dossier_versions_version_check CHECK (version>=1),
    CONSTRAINT calliope_user_dossier_versions_json_check CHECK (
        jsonb_typeof(context)='object' AND jsonb_typeof(evidence_receipts)='array'
    )
);
CREATE INDEX IF NOT EXISTS calliope_user_dossier_versions_created_idx
    ON rvbbit.calliope_user_dossier_versions (owner_email,created_at DESC);

COMMENT ON COLUMN rvbbit.calliope_dream_cycles.scope_kind IS
    'company is de-identified organizational reflection; personal is private to owner_email.';
COMMENT ON COLUMN rvbbit.calliope_dreams.semantic_key IS
    'Canonical subject/need/outcome identity used with model-declared prior matches to deepen paraphrases.';
COMMENT ON TABLE rvbbit.calliope_user_dossiers IS
    'Private, owner-scoped derived working context. Admin Team membership confers no visibility.';
COMMENT ON COLUMN rvbbit.calliope_user_dossiers.user_guidance IS
    'Explicit owner correction applied to future working-context distillation.';
