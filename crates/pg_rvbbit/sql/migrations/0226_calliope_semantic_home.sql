-- 0226: Calliope Semantic Home
--
-- A Home is a private, user-owned composition of authoritative warehouse
-- handles.  Items deliberately store locators rather than copied dashboard
-- markup or query results: artifacts can follow their latest version while a
-- named business object remains attached to the exact version, definition,
-- and dashboard context the user selected.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_boards (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    slug text NOT NULL,
    title text NOT NULL,
    kind text NOT NULL DEFAULT 'home',
    layout jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_boards_owner_slug_key UNIQUE (owner_email, slug),
    CONSTRAINT calliope_boards_kind_check CHECK (kind IN ('home'))
);

CREATE INDEX IF NOT EXISTS calliope_boards_owner_updated_idx
    ON rvbbit.calliope_boards (owner_email, updated_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_board_items (
    id uuid PRIMARY KEY,
    board_id uuid NOT NULL REFERENCES rvbbit.calliope_boards(id) ON DELETE CASCADE,
    item_kind text NOT NULL,
    canonical_key text NOT NULL,
    source jsonb NOT NULL,
    presentation jsonb NOT NULL DEFAULT '{}'::jsonb,
    sort_order bigint NOT NULL DEFAULT 1000,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_board_items_board_key UNIQUE (board_id, canonical_key),
    CONSTRAINT calliope_board_items_kind_check
        CHECK (item_kind IN ('artifact', 'artifact_object'))
);

CREATE INDEX IF NOT EXISTS calliope_board_items_board_order_idx
    ON rvbbit.calliope_board_items (board_id, sort_order, created_at);

COMMENT ON TABLE rvbbit.calliope_boards IS
    'Private user compositions. The home board is the durable working set shown above the public artifact gallery.';

COMMENT ON COLUMN rvbbit.calliope_board_items.source IS
    'Authoritative locator only: artifact slug/latest tracking, or exact artifact version plus semantic object definition and context.';

COMMENT ON COLUMN rvbbit.calliope_board_items.presentation IS
    'Non-authoritative display hints. Business meaning and replay values are re-resolved from the versioned artifact contract.';
