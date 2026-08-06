# Google Meet Brain connector

This connector turns generated Google Meet transcripts into governed Rvbbit
Brain documents. It is a polling baseline: Cloud Pub/Sub is optional and is not
required for correct synchronization. A future Workspace Events fast path would
use standard Cloud Pub/Sub (`pubsub.googleapis.com`), not Pub/Sub Lite.

## Google configuration

Enable the Google Meet REST API, Admin SDK API, Google Calendar API, and Google
Drive API. Use a Workspace service account with domain-wide delegation for:

```text
https://www.googleapis.com/auth/meetings.space.readonly
https://www.googleapis.com/auth/calendar.events.owned.readonly
https://www.googleapis.com/auth/admin.directory.user.readonly
https://www.googleapis.com/auth/drive.meet.readonly
```

`drive.meet.readonly` is recommended for transcript metadata and for auditing
the generated file's Google Drive grants. The default Brain authorization rule
is nevertheless the stricter Calendar policy below: only the organizer and
exact meeting invitee emails are granted access. Group, domain, `anyone`, and
manually-added Drive grants cannot widen that default.

The connector can also turn on automatic transcription for upcoming
Calendar-created meeting spaces. This is an external policy mutation, so it is
off by default. To opt in, add this delegated scope:

```text
https://www.googleapis.com/auth/meetings.space.settings
```

Then set `GMEET_AUTO_TRANSCRIBE=true`. Google applies the setting per meeting
space and starts transcription only when someone with permission to transcribe
joins; participants still receive Meet's normal transcription notice.

## Runtime

```text
GMEET_SA_KEY=/run/secrets/workspace-service-account.json
GMEET_ADMIN_SUBJECT=workspace-admin@example.com
GMEET_DOMAIN=example.com
GMEET_DISCOVER_USERS=true
GMEET_MAX_SUBJECTS=500
GMEET_LOOKBACK_DAYS=29
GMEET_CALENDAR_LOOKUP=true
GMEET_AUTO_TRANSCRIBE=false
GMEET_AUTO_TRANSCRIBE_DAYS=7
GMEET_DRIVE_ACL=true
GMEET_ACL_MODE=calendar_invitees_strict
CONNECTOR_TOKEN=<optional shared bearer token>
```

`CONNECTOR_TOKEN` is not issued by Google. Generate a private shared value (for
example, `openssl rand -hex 32`) and provide the same value to Postgres as
`GMEET_CONNECTOR_TOKEN`. Leaving it empty is supported only for a trusted,
unexposed Docker network.

Instead of directory discovery, set `GMEET_SUBJECTS` to a comma-separated list
of user emails. In `calendar_invitees_strict` mode, a transcript whose Calendar
event cannot be resolved fails closed to its known organizer (or to no users if
the organizer is also unknown). `drive` and `drive_and_calendar` remain explicit
legacy policy options for deployments that intentionally want artifact ACLs to
control or widen Brain access.

After the compose profile is running, create or update the Brain source:

```sql
SELECT rvbbit.brain_configure_google_meet_source();
SELECT rvbbit.brain_sync_dispatch(
  (SELECT source_id FROM rvbbit.brain_sources WHERE label = 'Google Meet'),
  'manual'
);
```

The same opt-in can be stored on the source instead of in container env:

```sql
SELECT rvbbit.brain_configure_google_meet_source(
  p_options => '{"auto_transcribe":true,"auto_transcribe_days":7}'::jsonb
);
```

The normal `rvbbit.brain_nightly()` job keeps it current. Meet structured
transcript entries expire after 30 days, so the source is append-safe: records
outside the polling window are retained instead of being interpreted as
deletions.

## Optional derived meeting briefs

Raw speaker-attributed transcripts remain canonical. After synchronization,
Rvbbit tries `rvbbit.clover_summarize(...)` first (including the older
`clover_llm_summarize` spelling), then `rvbbit.summarize(...)`. A successful
result becomes a separate `gmeet-summary:` Brain document with the same role and
per-user exclusions as its transcript, plus a `derived_from` lineage edge.

If neither function exists, or if the selected summarizer is unhealthy, raw
transcript synchronization still succeeds and no placeholder brief is created.
One systemic failure halts that summary batch so the nightly job does not repeat
the same failure across every meeting. Source options are:

```sql
SELECT rvbbit.brain_configure_google_meet_source(
  p_options => '{"summarize_meetings":true,"summary_max_docs":12}'::jsonb
);
```

## Catalog installation

The catalog entry is `integrations/google-meet-brain`. DataRabbit and SQL both
queue the same Warren image deployment and configure the Brain source:

```sql
SELECT rvbbit.deploy_catalog_capability(
  'integrations/google-meet-brain',
  '{"capability":true,"docker":true,"gpu":false}'::jsonb,
  NULL,
  'image'
);
```

Set the Google and connector environment values on the Warren host before
installing. Catalog deployment never copies service-account material into the
catalog or Postgres.
