-- 0231: private Daily Brief notes and user-scoped graph hints
--
-- Notes are deliberately append-only entries, not a mutable daily blob.  A
-- selected [[object]] becomes an edge in a private overlay that points at the
-- canonical Brain KG node without copying the note body into the shared graph.
-- Warehouse enforces the owner boundary before reading either table.

CREATE UNIQUE INDEX IF NOT EXISTS calliope_briefs_id_owner_idx
    ON rvbbit.calliope_briefs (id,owner_email);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_daily_notes (
    id uuid PRIMARY KEY,
    brief_id uuid NOT NULL,
    owner_email text NOT NULL,
    note_date date NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_daily_notes_body_check
        CHECK (char_length(btrim(body)) BETWEEN 1 AND 12000)
);
CREATE INDEX IF NOT EXISTS calliope_daily_notes_owner_date_idx
    ON rvbbit.calliope_daily_notes (owner_email,note_date DESC,created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_daily_notes_brief_created_idx
    ON rvbbit.calliope_daily_notes (brief_id,created_at,id);
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_daily_notes_brief_owner_fkey'
           AND conrelid='rvbbit.calliope_daily_notes'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_daily_notes
            ADD CONSTRAINT calliope_daily_notes_brief_owner_fkey
            FOREIGN KEY (brief_id,owner_email)
            REFERENCES rvbbit.calliope_briefs(id,owner_email) ON DELETE CASCADE;
    END IF;
END
$do$;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_daily_note_links (
    note_id uuid NOT NULL REFERENCES rvbbit.calliope_daily_notes(id) ON DELETE CASCADE,
    ordinal smallint NOT NULL,
    kg_node_id bigint REFERENCES rvbbit.kg_nodes(node_id) ON DELETE SET NULL,
    graph_id text NOT NULL DEFAULT 'brain',
    node_kind text NOT NULL,
    entity_kind text NOT NULL,
    label text NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (note_id,ordinal),
    CONSTRAINT calliope_daily_note_links_ordinal_check
        CHECK (ordinal BETWEEN 0 AND 23),
    CONSTRAINT calliope_daily_note_links_entity_kind_check
        CHECK (entity_kind IN ('person','place','thing','project','ticket'))
);
CREATE UNIQUE INDEX IF NOT EXISTS calliope_daily_note_links_node_idx
    ON rvbbit.calliope_daily_note_links (note_id,kg_node_id)
    WHERE kg_node_id IS NOT NULL;

-- This is the graph-shaped projection consumed by future Personal Briefs.  It
-- carries owner_email on every row and intentionally omits the private prose.
CREATE OR REPLACE VIEW rvbbit.calliope_private_note_edges AS
SELECT n.owner_email,
       n.note_date,
       n.id AS note_id,
       'daily_note'::text AS subject_kind,
       n.id::text AS subject_key,
       'mentions'::text AS predicate,
       l.entity_kind AS object_kind,
       coalesce(l.kg_node_id::text,lower(l.label)) AS object_key,
       l.kg_node_id,
       l.graph_id,
       l.node_kind,
       l.label,
       l.properties,
       n.created_at
  FROM rvbbit.calliope_daily_notes n
  JOIN rvbbit.calliope_daily_note_links l ON l.note_id=n.id;

COMMENT ON TABLE rvbbit.calliope_daily_notes IS
    'Private append-only notes attached to one user and one daily Calliope Brief.';
COMMENT ON TABLE rvbbit.calliope_daily_note_links IS
    'User-confirmed private overlay edges from a note to canonical Brain KG nodes.';
COMMENT ON VIEW rvbbit.calliope_private_note_edges IS
    'Owner-keyed private graph overlay; callers must filter by authenticated owner_email.';
