-- 0237: private Google Calendar context for Calliope Personal Briefs
--
-- Calendar consent is per user and separate from Google sign-in.  Refresh
-- tokens are encrypted by the Warehouse service before they reach this table.
-- Events remain an owner-keyed private overlay: they are never inserted into
-- shared Brain documents or KG tables.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_calendar_connections (
    owner_email text PRIMARY KEY,
    google_email text NOT NULL,
    refresh_token_ciphertext text NOT NULL,
    scopes text[] NOT NULL DEFAULT '{}'::text[],
    sync_token text,
    status text NOT NULL DEFAULT 'connected',
    connected_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_synced_at timestamptz,
    last_full_sync_at timestamptz,
    last_error text,
    CONSTRAINT calliope_google_calendar_connections_status_check
        CHECK (status IN ('connected','needs_reconnect','error'))
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_calendar_events (
    owner_email text NOT NULL REFERENCES rvbbit.calliope_google_calendar_connections(owner_email)
        ON DELETE CASCADE,
    calendar_id text NOT NULL DEFAULT 'primary',
    event_id text NOT NULL,
    recurring_event_id text,
    ical_uid text,
    status text NOT NULL DEFAULT 'confirmed',
    visibility text NOT NULL DEFAULT 'default',
    event_type text NOT NULL DEFAULT 'default',
    summary text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    location text NOT NULL DEFAULT '',
    html_link text,
    meeting_link text,
    organizer jsonb NOT NULL DEFAULT '{}'::jsonb,
    attendees jsonb NOT NULL DEFAULT '[]'::jsonb,
    starts_at timestamptz,
    ends_at timestamptz,
    all_day boolean NOT NULL DEFAULT false,
    response_status text,
    transparency text,
    etag text,
    google_updated_at timestamptz,
    synced_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_email,calendar_id,event_id),
    CONSTRAINT calliope_google_calendar_events_organizer_check
        CHECK (jsonb_typeof(organizer) = 'object'),
    CONSTRAINT calliope_google_calendar_events_attendees_check
        CHECK (jsonb_typeof(attendees) = 'array')
);

CREATE INDEX IF NOT EXISTS calliope_google_calendar_events_owner_start_idx
    ON rvbbit.calliope_google_calendar_events (owner_email,starts_at)
    WHERE status <> 'cancelled';
CREATE INDEX IF NOT EXISTS calliope_google_calendar_events_owner_updated_idx
    ON rvbbit.calliope_google_calendar_events (owner_email,google_updated_at DESC NULLS LAST);

-- A graph-shaped, owner-keyed projection for personal context.  It deliberately
-- omits event descriptions and never mutates the shared organization graph.
--
-- Keep the six canonical-match columns introduced by 0238 in this initial
-- shape as NULL placeholders.  The Warehouse application may have installed
-- the forward 0238 view before an older pg_rvbbit image records 0237 in
-- schema_migrations; replacing that 18-column view with the original
-- 12-column shape would fail with SQLSTATE 42P16.  Migration 0238 immediately
-- fills these columns with the real Brain lookup.
CREATE OR REPLACE VIEW rvbbit.calliope_private_calendar_edges AS
SELECT raw_edges.*,
       NULL::bigint AS kg_node_id,
       NULL::text AS graph_id,
       NULL::text AS node_kind,
       NULL::text AS canonical_label,
       NULL::text AS match_basis,
       NULL::double precision AS match_confidence
  FROM (
SELECT e.owner_email,
       e.calendar_id,
       e.event_id,
       'calendar_event'::text AS subject_kind,
       e.event_id AS subject_key,
       'involves'::text AS predicate,
       'person'::text AS object_kind,
       lower(a.item->>'email') AS object_key,
       coalesce(nullif(a.item->>'display_name',''),a.item->>'email') AS label,
       jsonb_strip_nulls(jsonb_build_object(
           'email',lower(a.item->>'email'),
           'response_status',a.item->>'response_status',
           'organizer',(a.item->>'organizer')::boolean,
           'self',(a.item->>'self')::boolean
       )) AS properties,
       e.starts_at,
       e.ends_at
  FROM rvbbit.calliope_google_calendar_events e
 CROSS JOIN LATERAL jsonb_array_elements(e.attendees) AS a(item)
 WHERE e.status <> 'cancelled'
   AND nullif(a.item->>'email','') IS NOT NULL
UNION ALL
SELECT e.owner_email,e.calendar_id,e.event_id,
       'calendar_event'::text,e.event_id,
       'organized_by'::text,'person'::text,
       lower(e.organizer->>'email'),
       coalesce(nullif(e.organizer->>'display_name',''),e.organizer->>'email'),
       jsonb_strip_nulls(jsonb_build_object('email',lower(e.organizer->>'email'))),
       e.starts_at,e.ends_at
  FROM rvbbit.calliope_google_calendar_events e
 WHERE e.status <> 'cancelled'
   AND nullif(e.organizer->>'email','') IS NOT NULL
UNION ALL
SELECT e.owner_email,e.calendar_id,e.event_id,
       'calendar_event'::text,e.event_id,
       'at'::text,'place'::text,
       lower(e.location),e.location,
       jsonb_build_object('label',e.location),
       e.starts_at,e.ends_at
  FROM rvbbit.calliope_google_calendar_events e
 WHERE e.status <> 'cancelled' AND nullif(e.location,'') IS NOT NULL
  ) raw_edges;

COMMENT ON TABLE rvbbit.calliope_google_calendar_connections IS
    'Private per-user Google Calendar grant state; refresh_token_ciphertext is application-encrypted.';
COMMENT ON TABLE rvbbit.calliope_google_calendar_events IS
    'Bounded, normalized owner-private Calendar cache for Personal Briefs; no raw Google event payloads.';
COMMENT ON VIEW rvbbit.calliope_private_calendar_edges IS
    'Owner-keyed personal graph overlay for Calendar people and places; callers must filter by owner_email.';
