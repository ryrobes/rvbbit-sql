-- 0242: Separate the quiet Dream candidate reservoir from the promoted shelf.
--
-- A reflection may now retain more hypotheses than the three it presents to
-- people.  Existing Dreams remain promoted so this migration never hides an
-- already-visible idea; subsequent cycles curate proposed Dreams explicitly.

ALTER TABLE rvbbit.calliope_dream_cycles
    ADD COLUMN IF NOT EXISTS candidate_count integer NOT NULL DEFAULT 0;

UPDATE rvbbit.calliope_dream_cycles
   SET candidate_count = dream_count
 WHERE candidate_count = 0 AND dream_count > 0;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dream_cycles_candidate_count_check'
           AND conrelid='rvbbit.calliope_dream_cycles'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dream_cycles
            ADD CONSTRAINT calliope_dream_cycles_candidate_count_check
            CHECK (candidate_count >= 0 AND dream_count <= candidate_count);
    END IF;
END
$do$;

ALTER TABLE rvbbit.calliope_dreams
    ADD COLUMN IF NOT EXISTS portfolio_state text NOT NULL DEFAULT 'promoted',
    ADD COLUMN IF NOT EXISTS portfolio_rank smallint,
    ADD COLUMN IF NOT EXISTS rank_score numeric(5,4) NOT NULL DEFAULT 0.5,
    ADD COLUMN IF NOT EXISTS promoted_at timestamptz;

UPDATE rvbbit.calliope_dreams
   SET promoted_at = coalesce(promoted_at,updated_at,created_at)
 WHERE portfolio_state='promoted' AND promoted_at IS NULL;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_portfolio_state_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_portfolio_state_check
            CHECK (portfolio_state IN ('promoted','backlog'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_portfolio_rank_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_portfolio_rank_check
            CHECK (portfolio_rank IS NULL OR portfolio_rank BETWEEN 1 AND 3);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_rank_score_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_rank_score_check
            CHECK (rank_score BETWEEN 0 AND 1);
    END IF;
END
$do$;

CREATE INDEX IF NOT EXISTS calliope_dreams_portfolio_idx
    ON rvbbit.calliope_dreams
       (portfolio_state,status,portfolio_rank NULLS LAST,rank_score DESC,updated_at DESC);

COMMENT ON COLUMN rvbbit.calliope_dream_cycles.candidate_count IS
    'All evidence-backed candidates retained by this reflection; dream_count is the promoted subset.';
COMMENT ON COLUMN rvbbit.calliope_dreams.portfolio_state IS
    'promoted appears on the small editorial shelf; backlog remains quietly inspectable in the wings.';
COMMENT ON COLUMN rvbbit.calliope_dreams.rank_score IS
    'Bounded evidence, novelty, feasibility, impact, and recurrence editorial score.';
