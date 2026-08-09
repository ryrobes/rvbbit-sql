# Rvbbit Time Travel

Rvbbit time travel is generation-based. Each successful acceleration
refresh writes immutable parquet row groups and stamps them with a
monotonic generation number for the table. Historical reads set an
`AS OF` generation before running normal SQL; the native scan and
DataFusion catalog discovery then ignore row groups newer than that
generation.

The heap remains the PostgreSQL source of truth for latest reads,
`pg_dump`, and rebuilds. Time-travel reads are over the accelerated
parquet layer, so a table needs at least one successful
`rvbbit.refresh_acceleration(...)` or `rvbbit.compact(...)` before it
has historical generations to read.

## Generation And History Policies

Generation composition and history retention are separate table policies:

- `generation_semantics = 'cumulative'` means generations are deltas whose
  visible union is the current table. This is the default for ordinary
  `refresh_acceleration(...)` workloads.
- `generation_semantics = 'snapshot'` means each generation is a complete
  replacement. `snapshot_load(...)` sets this automatically.
- `history_policy = 'current'` is the default for newly registered tables. It
  disables historical reads, permits superseded complete snapshots to be
  retired immediately, and treats a full-table replacement as an invalidation
  boundary instead of generating one tombstone per old row.
- `history_policy = 'retained'` is the explicit opt-in for AS OF history.
  Upgrades preserve existing tables as retained; only newly registered tables
  receive the current-only default. RVBBIT cubes opt in automatically because
  their refresh timeline is part of the cube contract.

Inspect the effective policy and its physical footprint with:

```sql
SELECT *
FROM rvbbit.acceleration_storage_policy
WHERE table_oid = 'orders'::regclass;
```

Current-only tables accept ordinary PostgreSQL replacement ETL; loaders do not
need RVBBIT-specific syntax:

```sql
BEGIN;
TRUNCATE orders;
COPY orders FROM STDIN;
COMMIT;
```

After commit, latest reads use the authoritative heap until
`rebuild_acceleration(...)` publishes a new baseline. A non-manual
`accel_tick()` policy recognizes this state as `current replacement pending`
and always chooses a full rebuild, even when the old and new row counts match.
The TRUNCATE itself creates no row tombstones.

For callers that explicitly want one SQL operation to load and synchronously
publish the replacement accelerator, the convenience helper remains available:

```sql
SELECT *
FROM rvbbit.snapshot_load_current(
    'orders'::regclass,
    $$SELECT id, amount FROM staging.orders$$
);
```

Only the newly published snapshot remains in the row-group catalog. Old files
enter the ordinary orphan queue so readers already using the prior catalog
snapshot can finish before physical deletion.

An existing snapshot table can opt out of history explicitly:

```sql
SELECT rvbbit.set_acceleration_storage_policy(
    'orders'::regclass,
    history_policy => 'current'
);
```

For a cumulative table, `current` disables AS OF but does **not** immediately
drop old generations or tombstones created by individual UPDATE/DELETE
operations: older generations may still contain rows that are part of the
current table. Those row-level overlays keep acceleration online until the next
fold. A full-table TRUNCATE is different: it invalidates the old baseline as a
unit, creates no tombstones, and temporarily routes to the heap. The policy
result reports `fold_required = true` when a full
`rebuild_acceleration(...)` is needed to
materialize one current baseline and clear those correctness overlays.
Changing generation semantics on an already-materialized table is rejected;
publish a complete snapshot or perform a full conversion instead.

## Inspect Generations

```sql
SELECT *
FROM rvbbit.list_generations('orders'::regclass);
```

This returns the table timeline, newest first:

```text
 generation | committed_at              | n_rows | n_row_groups
------------+---------------------------+--------+-------------
          4 | 2026-05-28 02:30:12+00    | 100000 |           8
          3 | 2026-05-28 02:22:51+00    |   2500 |           1
```

For just the current generation:

```sql
SELECT rvbbit.current_generation('orders'::regclass);
```

For UI timelines, use the metadata-only helper:

```sql
SELECT *
FROM rvbbit.time_travel_timeline('orders'::regclass);
```

It returns one row per available snapshot tick:

```text
 generation | committed_at           | rows_written | row_groups_written | visible_rows_estimate | visible_row_groups | tombstones_visible
------------+------------------------+--------------+--------------------+-----------------------+--------------------+-------------------
          4 | 2026-05-28 02:30:12+00 |       100000 |                  8 |                352000 |                 18 |                12
          3 | 2026-05-28 02:22:51+00 |         2500 |                  1 |                252000 |                 10 |                 0
```

`rows_written` is the delta written at that generation.
`visible_rows_estimate` is derived from row-group metadata minus
generation-visible tombstones. The helper does not scan heap or parquet.

## Native AS OF Generation

The lowest-level control is the `rvbbit.as_of_generation` GUC:

```sql
BEGIN;
SET LOCAL rvbbit.as_of_generation = '3';

SELECT customer_id, sum(amount)
FROM orders
GROUP BY customer_id;

COMMIT;
```

Use `SET LOCAL` inside a transaction when you want the setting to apply
only to one scoped read. A positive value means "read row groups with
`generation <= value`." Unset, empty, zero, and negative values mean
"latest."

For session-level use:

```sql
SET rvbbit.as_of_generation = '3';
SELECT count(*) FROM orders;
RESET rvbbit.as_of_generation;
```

## Timestamp Helper

Wall-clock reads resolve through `rvbbit.generations.committed_at`.
`rvbbit.set_as_of(...)` finds the newest generation committed at or
before the timestamp, sets `rvbbit.as_of_generation`, and returns the
generation it selected:

```sql
SELECT rvbbit.set_as_of(
    'orders'::regclass,
    '2026-05-28 02:25:00+00'::timestamptz
);

SELECT count(*), avg(amount)
FROM orders
WHERE status = 'ok';

SELECT rvbbit.set_as_of_reset();
```

This helper sets the GUC at session scope. Always call
`rvbbit.set_as_of_reset()` when the next statements should read the
latest table state.

For this helper, timestamps earlier than the first recorded generation
resolve to generation `0`, which the legacy generation GUC treats like
"latest." Check `rvbbit.list_generations(...)` first when an exact
historical boundary matters.

## Comment AS OF Shorthand

Use a leading SQL comment when you want a single statement to read as of
a timestamp without exposing generation numbers:

```sql
/* rvbbit: as_of = '2026-05-28 02:25:00+00' */
SELECT *
FROM orders
WHERE status = 'ok';
```

Line comments work too:

```sql
-- rvbbit: as_of = '2026-05-28 02:25:00+00'
SELECT count(*) FROM orders;
```

The comment is whole-query scope, similar to `SET LOCAL`: every rvbbit
table in the statement is resolved against the same timestamp. Each table
still resolves that timestamp to its own generation internally.

For comment AS OF, a timestamp before the first recorded generation
resolves to generation `0` for row-group filtering. Current row groups
use positive generations, so that normally means no accelerated rows are
visible at that point.

The lower-level helper surface is still useful when you want several
statements in one session to share the same snapshot:

```sql
SELECT rvbbit.set_as_of('orders'::regclass, now() - interval '1 hour');
SELECT * FROM orders WHERE status = 'ok';
SELECT rvbbit.set_as_of_reset();
```

The PostgreSQL parser-level table clause form is not currently
supported:

```sql
-- Not supported today: PostgreSQL rejects this before extension hooks run.
SELECT * FROM orders AS OF GENERATION 3;
SELECT * FROM orders AS OF TIMESTAMP '2026-05-28 02:25:00+00';
```

If parser-level syntax is added later, it should compile down to the
same timestamp-to-generation machinery so the executor semantics stay
identical.

## Deletes And Updates

Rvbbit delete/update support uses merge-on-read tombstones. Tombstones
carry their own `deleted_generation`.

At an AS OF generation:

- Row groups with `generation > as_of_generation` are invisible.
- Tombstones with `deleted_generation > as_of_generation` are ignored.
- Tombstones with `deleted_generation <= as_of_generation` are applied.

That means a row deleted in generation 5 is still visible at AS OF
generation 4, and hidden at AS OF generation 5 or later.

Useful inspection helper:

```sql
SELECT rvbbit.tombstone_count('orders'::regclass, 4);
```

## Executor Behavior

Both main execution paths honor the same resolved snapshot:

- Native custom scan filters `rvbbit.row_groups` to the requested
  generation and loads delete bitmaps at scan start.
- In-process DataFusion registers only row-group files visible at the
  requested generation.

Hot in-memory tables and layout variants are intentionally skipped for
AS OF reads unless they can prove generation correctness. The canonical
parquet row-group layout is the authoritative historical layer.

## Minimal Example

```sql
CREATE TABLE orders (id int, amount numeric) USING rvbbit;

INSERT INTO orders VALUES (1, 10), (2, 20);
SELECT rvbbit.refresh_acceleration('orders'::regclass, false);

INSERT INTO orders VALUES (3, 30);
SELECT rvbbit.refresh_acceleration('orders'::regclass, false);

SELECT * FROM rvbbit.list_generations('orders'::regclass);

BEGIN;
SET LOCAL rvbbit.as_of_generation = '1';
SELECT count(*), max(id) FROM orders; -- 2, 2
COMMIT;

SELECT count(*), max(id) FROM orders; -- 3, 3
```
