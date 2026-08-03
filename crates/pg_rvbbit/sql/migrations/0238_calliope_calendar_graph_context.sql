-- 0238: reconcile private Calendar edge hints with canonical Brain objects
--
-- Calendar events and their prose remain an owner-keyed private overlay.  This
-- view upgrade only appends an exact-match candidate for explicit attendee,
-- organizer, and location values.  Consumers must still apply
-- brain_visible_docs(owner_email) before exposing the canonical node.

CREATE OR REPLACE VIEW rvbbit.calliope_private_calendar_edges AS
WITH raw_edges AS (
    SELECT e.owner_email,e.calendar_id,e.event_id,
           'calendar_event'::text AS subject_kind,e.event_id AS subject_key,
           'involves'::text AS predicate,'person'::text AS object_kind,
           lower(a.item->>'email') AS object_key,
           coalesce(nullif(a.item->>'display_name',''),a.item->>'email') AS label,
           jsonb_strip_nulls(jsonb_build_object(
               'email',lower(a.item->>'email'),
               'response_status',a.item->>'response_status',
               'organizer',(a.item->>'organizer')::boolean,
               'self',(a.item->>'self')::boolean
           )) AS properties,e.starts_at,e.ends_at
      FROM rvbbit.calliope_google_calendar_events e
 CROSS JOIN LATERAL jsonb_array_elements(e.attendees) AS a(item)
     WHERE e.status <> 'cancelled' AND nullif(a.item->>'email','') IS NOT NULL
    UNION ALL
    SELECT e.owner_email,e.calendar_id,e.event_id,
           'calendar_event'::text,e.event_id,'organized_by'::text,'person'::text,
           lower(e.organizer->>'email'),
           coalesce(nullif(e.organizer->>'display_name',''),e.organizer->>'email'),
           jsonb_strip_nulls(jsonb_build_object('email',lower(e.organizer->>'email'))),
           e.starts_at,e.ends_at
      FROM rvbbit.calliope_google_calendar_events e
     WHERE e.status <> 'cancelled' AND nullif(e.organizer->>'email','') IS NOT NULL
    UNION ALL
    SELECT e.owner_email,e.calendar_id,e.event_id,
           'calendar_event'::text,e.event_id,'at'::text,'place'::text,
           lower(e.location),e.location,jsonb_build_object('label',e.location),
           e.starts_at,e.ends_at
      FROM rvbbit.calliope_google_calendar_events e
     WHERE e.status <> 'cancelled' AND nullif(e.location,'') IS NOT NULL
)
SELECT r.owner_email,r.calendar_id,r.event_id,r.subject_kind,r.subject_key,
       r.predicate,r.object_kind,r.object_key,r.label,r.properties,
       r.starts_at,r.ends_at,
       resolved.node_id AS kg_node_id,
       resolved.graph_id,
       resolved.kind AS node_kind,
       resolved.label AS canonical_label,
       resolved.match_basis,
       resolved.match_confidence
  FROM raw_edges r
  LEFT JOIN LATERAL (
        WITH direct_lookup(match_value,match_basis,match_priority,match_confidence) AS (
            VALUES
              (rvbbit.kg_normalize_label(r.object_key),'object_key'::text,0,1.0::double precision),
              (rvbbit.kg_normalize_label(r.label),'display_label'::text,2,0.99::double precision)
        ),
        alias_lookup(match_value,match_basis,match_priority,match_confidence) AS (
            VALUES
              (rvbbit.kg_normalize_label(r.object_key),'object_key_alias'::text,1,1.0::double precision),
              (rvbbit.kg_normalize_label(r.label),'display_alias'::text,3,0.98::double precision)
        ),
        candidates AS (
            SELECT n.node_id,n.graph_id,n.kind,n.label,n.confidence,
                   lookup.match_basis,lookup.match_priority,lookup.match_confidence
              FROM direct_lookup lookup
              JOIN rvbbit.kg_nodes n
                ON n.graph_id='brain' AND n.label_norm=lookup.match_value
            UNION ALL
            SELECT n.node_id,n.graph_id,n.kind,n.label,n.confidence,
                   lookup.match_basis,lookup.match_priority,lookup.match_confidence
              FROM alias_lookup lookup
              JOIN rvbbit.kg_aliases a
                ON a.graph_id='brain' AND a.alias_norm=lookup.match_value
              JOIN rvbbit.kg_nodes n ON n.node_id=a.node_id AND n.graph_id='brain'
        )
        SELECT c.node_id,c.graph_id,c.kind,c.label,c.match_basis,c.match_confidence
          FROM candidates c
         WHERE (
             (r.object_kind='person' AND c.kind IN
               ('person','people','employee','user','contact','assignee','owner'))
             OR
             (r.object_kind='place' AND c.kind IN
               ('place','location','city','country','state','region','address',
                'venue','office','site'))
         )
         ORDER BY c.match_priority,c.confidence DESC,c.node_id
         LIMIT 1
  ) resolved ON true;

COMMENT ON VIEW rvbbit.calliope_private_calendar_edges IS
    'Owner-keyed Calendar graph overlay with exact canonical Brain candidates; callers must filter owner_email and enforce Brain document visibility before exposing kg_node_id.';
