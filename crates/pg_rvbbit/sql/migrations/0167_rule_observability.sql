-- 0167: rule observability — two planes (KIT_PLATES_PLAN §18).
--
-- Rules deliberately do NOT ride the receipt system (set-based evaluation
-- would firehose it — the delete_log lesson). Instead:
--
--   LIVE plane — kit_rule_sets registers each set's subject_sql; the
--   distribution helper re-evaluates rules over CURRENT data on demand.
--   Read-only safe, always true, no storage, no staleness.
--
--   PERSISTENT plane — kit_rule_stats (one bounded row per rule) and
--   kit_rule_log (errors always; full trace only in debug mode) capture
--   write-context evaluations. Plate renders run in READ ONLY transactions
--   and are skipped via a cheap GUC check — NOT caught exceptions, which
--   would cost a subtransaction per row on hot paths.
--
-- GUCs: rvbbit.rule_stats = on(default)|off · rvbbit.rule_log =
-- errors(default)|all|off ('all' is the debug trace for small sets).

CREATE TABLE IF NOT EXISTS rvbbit.kit_rule_sets (
    kit         text NOT NULL,
    rule_set    text NOT NULL,
    subject_sql text NOT NULL,   -- SELECT producing this set's subject rows
    description text,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (kit, rule_set)
);

CREATE OR REPLACE FUNCTION rvbbit.upsert_kit_rule_set(
    p_kit text,
    p_rule_set text,
    p_subject_sql text,
    p_description text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $ukrs$
BEGIN
    IF p_subject_sql !~* '^[[:space:]]*(SELECT|WITH)\y' THEN
        RAISE EXCEPTION 'rule set % subject_sql must be SELECT-shaped', p_rule_set;
    END IF;
    INSERT INTO rvbbit.kit_rule_sets (kit, rule_set, subject_sql, description)
    VALUES (p_kit, p_rule_set, p_subject_sql, p_description)
    ON CONFLICT (kit, rule_set) DO UPDATE SET
        subject_sql = EXCLUDED.subject_sql,
        description = EXCLUDED.description,
        updated_at = clock_timestamp();
END
$ukrs$;

CREATE TABLE IF NOT EXISTS rvbbit.kit_rule_stats (
    kit             text NOT NULL,
    rule_set        text NOT NULL,
    rule_id         text NOT NULL,   -- '(no match)' counts fall-throughs
    matches         bigint NOT NULL DEFAULT 0,
    errors          bigint NOT NULL DEFAULT 0,
    last_matched_at timestamptz,
    last_error_at   timestamptz,
    last_error      text,
    last_subject    jsonb,           -- latest matching subject: the specimen
    PRIMARY KEY (kit, rule_set, rule_id)
);

CREATE TABLE IF NOT EXISTS rvbbit.kit_rule_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kit        text NOT NULL,
    rule_set   text NOT NULL,
    rule_id    text,
    outcome    text NOT NULL,        -- 'matched' | 'error'
    subject    jsonb,
    error      text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS kit_rule_log_created_idx ON rvbbit.kit_rule_log (created_at);

CREATE OR REPLACE FUNCTION rvbbit.prune_kit_rule_log(p_keep interval DEFAULT '30 days')
RETURNS bigint
LANGUAGE sql
AS $prl$
    WITH gone AS (
        DELETE FROM rvbbit.kit_rule_log
        WHERE created_at < clock_timestamp() - p_keep
        RETURNING 1
    ) SELECT count(*) FROM gone;
$prl$;

-- rule_verdict v2: VOLATILE + instrumented. Semantics unchanged: first match
-- by priority wins, broken rules win loudly with {"rule_error": true}.
CREATE OR REPLACE FUNCTION rvbbit.rule_verdict(
    p_kit text,
    p_rule_set text,
    p_subject jsonb
) RETURNS TABLE (rule_id text, verdict jsonb)
LANGUAGE plpgsql VOLATILE
AS $rv$
#variable_conflict use_column
DECLARE
    r record;
    v_hit boolean;
    v_writable boolean := current_setting('transaction_read_only', true) IS DISTINCT FROM 'on';
    v_stats boolean;
    v_log text;
BEGIN
    v_stats := v_writable AND coalesce(current_setting('rvbbit.rule_stats', true), 'on') <> 'off';
    v_log := CASE WHEN v_writable THEN coalesce(nullif(current_setting('rvbbit.rule_log', true), ''), 'errors') ELSE 'off' END;

    FOR r IN
        SELECT k.rule_id, k.when_sql, k.verdict
        FROM rvbbit.kit_rules k
        WHERE k.kit = p_kit AND k.rule_set = p_rule_set AND k.active
        ORDER BY k.priority, k.rule_id
    LOOP
        BEGIN
            EXECUTE 'SELECT (' || r.when_sql || ') FROM (SELECT $1::jsonb AS subject) _s'
                INTO v_hit USING p_subject;
        EXCEPTION WHEN others THEN
            IF v_stats THEN
                INSERT INTO rvbbit.kit_rule_stats AS s (kit, rule_set, rule_id, errors, last_error_at, last_error)
                VALUES (p_kit, p_rule_set, r.rule_id, 1, clock_timestamp(), SQLERRM)
                ON CONFLICT (kit, rule_set, rule_id) DO UPDATE SET
                    errors = s.errors + 1, last_error_at = clock_timestamp(), last_error = EXCLUDED.last_error;
            END IF;
            IF v_log IN ('errors', 'all') THEN
                INSERT INTO rvbbit.kit_rule_log (kit, rule_set, rule_id, outcome, subject, error)
                VALUES (p_kit, p_rule_set, r.rule_id, 'error', p_subject, SQLERRM);
            END IF;
            rule_id := r.rule_id;
            verdict := jsonb_build_object('rule_error', true, 'error', SQLERRM);
            RETURN NEXT;
            RETURN;
        END;
        IF v_hit THEN
            IF v_stats THEN
                INSERT INTO rvbbit.kit_rule_stats AS s (kit, rule_set, rule_id, matches, last_matched_at, last_subject)
                VALUES (p_kit, p_rule_set, r.rule_id, 1, clock_timestamp(), p_subject)
                ON CONFLICT (kit, rule_set, rule_id) DO UPDATE SET
                    matches = s.matches + 1, last_matched_at = clock_timestamp(), last_subject = EXCLUDED.last_subject;
            END IF;
            IF v_log = 'all' THEN
                INSERT INTO rvbbit.kit_rule_log (kit, rule_set, rule_id, outcome, subject)
                VALUES (p_kit, p_rule_set, r.rule_id, 'matched', p_subject);
            END IF;
            rule_id := r.rule_id;
            verdict := r.verdict;
            RETURN NEXT;
            RETURN;
        END IF;
    END LOOP;

    IF v_stats THEN
        INSERT INTO rvbbit.kit_rule_stats AS s (kit, rule_set, rule_id, matches, last_matched_at, last_subject)
        VALUES (p_kit, p_rule_set, '(no match)', 1, clock_timestamp(), p_subject)
        ON CONFLICT (kit, rule_set, rule_id) DO UPDATE SET
            matches = s.matches + 1, last_matched_at = clock_timestamp(), last_subject = EXCLUDED.last_subject;
    END IF;
END
$rv$;

-- LIVE plane: current-truth distribution of a rule set over its registered
-- subject rows. Read-only safe (instrumentation self-disables), generic —
-- this is what the system/rules plate renders.
CREATE OR REPLACE FUNCTION rvbbit.rule_set_distribution(p_kit text, p_rule_set text)
RETURNS TABLE (rule_id text, verdict jsonb, matches bigint)
LANGUAGE plpgsql
AS $rsd$
DECLARE
    v_subject_sql text;
BEGIN
    SELECT rs.subject_sql INTO v_subject_sql
    FROM rvbbit.kit_rule_sets rs WHERE rs.kit = p_kit AND rs.rule_set = p_rule_set;
    IF v_subject_sql IS NULL THEN
        RAISE EXCEPTION 'rule_set_distribution: no subject_sql registered for %/% (rvbbit.upsert_kit_rule_set)', p_kit, p_rule_set;
    END IF;
    RETURN QUERY EXECUTE
        'SELECT coalesce(v.rule_id, ''(no match)'') AS rule_id, v.verdict, count(*)::bigint AS matches
         FROM (' || v_subject_sql || ') s
         LEFT JOIN LATERAL rvbbit.rule_verdict($1, $2, to_jsonb(s)) v ON true
         GROUP BY 1, 2 ORDER BY 3 DESC'
    USING p_kit, p_rule_set;
END
$rsd$;

-- ── system/rules: the observability plate (ships with the product) ──
SELECT rvbbit.upsert_plate(
  'system/rules',
  'Rule Observability',
  $tpl$
<div class="plate-section">
  <div class="plate-toolbar">
    <label class="plate-field">kit
      <select rv-emit="kit" query="kit_opts" value="kit" label="kit"></select>
    </label>
  </div>
  <h2>Decision tables — {{ params.kit }}</h2>
  <div rv-if="empty.none">
    <p>No rule sets registered for this kit. Register a subject with <code>rvbbit.upsert_kit_rule_set()</code> to light up live distributions.</p>
  </div>
</div>

<div class="plate-section">
  <h3>Live distribution (evaluated over current data, right now)</h3>
  <table class="plate-table">
    <thead><tr><th>rule set</th><th>rule</th><th>label</th><th>rows</th><th>share</th></tr></thead>
    <tbody>
      <tr rv-each="live">
        <td><code>{{ row.rule_set }}</code></td>
        <td><code>{{ row.rule_id }}</code></td>
        <td><span class="plate-chip {{ row.tone }}">{{ row.label }}</span></td>
        <td>{{ row.matches }}</td>
        <td>{{ row.share }}</td>
      </tr>
    </tbody>
  </table>
</div>

<div class="plate-section">
  <h3>The rules (dead rules and errors surface here)</h3>
  <table class="plate-table">
    <thead><tr><th>set</th><th>prio</th><th>rule</th><th>when</th><th>recorded matches</th><th>errors</th><th>status</th></tr></thead>
    <tbody>
      <tr rv-each="rules">
        <td><code>{{ row.rule_set }}</code></td>
        <td>{{ row.priority }}</td>
        <td><code>{{ row.rule_id }}</code></td>
        <td><code>{{ row.when_short }}</code></td>
        <td>{{ row.matches }}</td>
        <td>{{ row.errors }}</td>
        <td><span class="plate-chip {{ row.tone }}" title="{{ row.status_detail }}">{{ row.status }}</span></td>
      </tr>
    </tbody>
  </table>
  <div rv-each="stats_note"><p class="plate-row-flag" rv-if="row.show">{{ row.msg }}</p></div>
</div>

<div class="plate-section">
  <h3>Recent log (errors always; matches only in debug mode)</h3>
  <table class="plate-table">
    <thead><tr><th>when</th><th>set</th><th>rule</th><th>outcome</th><th>detail</th></tr></thead>
    <tbody>
      <tr rv-each="log">
        <td>{{ row.at }}</td>
        <td><code>{{ row.rule_set }}</code></td>
        <td><code>{{ row.rule_id }}</code></td>
        <td><span class="plate-chip {{ row.tone }}">{{ row.outcome }}</span></td>
        <td>{{ row.detail }}</td>
      </tr>
    </tbody>
  </table>
  <div rv-each="remedies">
    <p>
      <button type="button" rv-open-sql="{{ row.debug_script }}" rv-open-sql-title="Rule debug trace">Debug trace SQL</button>
      <button type="button" rv-open-sql="{{ row.prune_script }}" rv-open-sql-title="Prune rule log">Prune log</button>
    </p>
  </div>
</div>
$tpl$,
  jsonb_build_object(
    'kit_opts', jsonb_build_object('sql', $q$
SELECT DISTINCT kit FROM rvbbit.kit_rules ORDER BY kit
    $q$),
    'empty', jsonb_build_object('sql', $q$
SELECT NOT EXISTS (SELECT 1 FROM rvbbit.kit_rule_sets WHERE kit = {{ params.kit }}) AS none
    $q$),
    'live', jsonb_build_object('sql', $q$
WITH d AS (
  SELECT rs.rule_set, x.rule_id, x.verdict, x.matches
  FROM rvbbit.kit_rule_sets rs
  CROSS JOIN LATERAL rvbbit.rule_set_distribution(rs.kit, rs.rule_set) x
  WHERE rs.kit = {{ params.kit }}
)
SELECT rule_set, rule_id,
       coalesce(verdict->>'label', rule_id) AS label,
       CASE coalesce(verdict->>'tone', '') WHEN 'bad' THEN 'bad' WHEN 'warn' THEN 'warn' ELSE 'ok' END AS tone,
       matches,
       round(100.0 * matches / greatest(sum(matches) OVER (PARTITION BY rule_set), 1), 1) || '%' AS share
FROM d ORDER BY rule_set, matches DESC
    $q$),
    'rules', jsonb_build_object('sql', $q$
SELECT r.rule_set, r.priority, r.rule_id,
       left(btrim(r.when_sql), 46) || CASE WHEN length(btrim(r.when_sql)) > 46 THEN '…' ELSE '' END AS when_short,
       coalesce(s.matches, 0) AS matches,
       coalesce(s.errors, 0) AS errors,
       CASE WHEN coalesce(s.errors, 0) > 0 THEN 'bad'
            WHEN coalesce(s.matches, 0) = 0 THEN 'warn' ELSE 'ok' END AS tone,
       CASE WHEN coalesce(s.errors, 0) > 0 THEN 'erroring'
            WHEN coalesce(s.matches, 0) = 0 THEN 'no recorded hits' ELSE 'healthy' END AS status,
       CASE WHEN s.last_error IS NOT NULL THEN 'last error: ' || s.last_error
            WHEN s.last_matched_at IS NOT NULL THEN 'last matched ' || to_char(s.last_matched_at, 'YYYY-MM-DD HH24:MI')
            ELSE 'never recorded in a write context — live distribution is the truth' END AS status_detail
FROM rvbbit.kit_rules r
LEFT JOIN rvbbit.kit_rule_stats s USING (kit, rule_set, rule_id)
WHERE r.kit = {{ params.kit }} AND r.active
ORDER BY r.rule_set, r.priority
    $q$),
    'stats_note', jsonb_build_object('sql', $q$
SELECT true AS show,
       'Recorded counts come from write-context evaluations (actions, flows, cron). Plate renders run read-only and are not counted — the live distribution above is always current truth.' AS msg
    $q$),
    'log', jsonb_build_object('sql', $q$
SELECT to_char(created_at, 'MM-DD HH24:MI:SS') AS at, rule_set, coalesce(rule_id, '—') AS rule_id, outcome,
       CASE WHEN outcome = 'error' THEN 'bad' ELSE 'ok' END AS tone,
       coalesce(error, left(subject::text, 60)) AS detail
FROM rvbbit.kit_rule_log
WHERE kit = {{ params.kit }}
ORDER BY id DESC LIMIT 15
    $q$),
    'remedies', jsonb_build_object('sql', $q$
SELECT E'-- Debug trace: log EVERY rule evaluation this session (small sets only).\nSET rvbbit.rule_log = ''all'';\n-- … run your action/flow, then inspect:\nSELECT * FROM rvbbit.kit_rule_log ORDER BY id DESC LIMIT 50;\n-- and turn it back down:\nSET rvbbit.rule_log = ''errors'';' AS debug_script,
       E'-- Drop rule-log entries older than 30 days.\nSELECT rvbbit.prune_kit_rule_log(''30 days''::interval);' AS prune_script
    $q$)
  ),
  '{}'::jsonb,
  '[{"name": "kit", "default": "field-kit"}]'::jsonb,
  'rvbbit', 'Decision-table observability: live distributions, dead-rule and error surfacing, debug trace'
);

-- export_kit v3: rule-set subject registrations travel too (the live
-- observability plane arrives with the kit).
CREATE OR REPLACE FUNCTION rvbbit.export_kit(p_kit text)
RETURNS text
LANGUAGE plpgsql
AS $ek$
DECLARE
    k rvbbit.kits%ROWTYPE;
    v_out text;
    v_plates text;
    v_modules text;
    v_contracts text;
    v_rules text;
    v_rule_sets text;
    v_ops text;
BEGIN
    SELECT * INTO k FROM rvbbit.kits WHERE kit = p_kit;
    IF NOT FOUND THEN
        IF NOT EXISTS (SELECT 1 FROM rvbbit.plates WHERE kit = p_kit) THEN
            RAISE EXCEPTION 'export_kit: no kit named %', p_kit;
        END IF;
        k.kit := p_kit;
        k.version := '0.0.0';
        k.title := p_kit;
        k.description := NULL;
        k.setup_sql := NULL;
    END IF;

    v_out := format(E'-- rvbbit kit: %s v%s\n-- generated by rvbbit.export_kit() · api rvbbit.kit/v1\n-- Install: run this whole file in ONE transaction (validate with ROLLBACK first).\n\n',
                    k.kit, k.version);

    v_out := v_out || format(E'SELECT rvbbit.upsert_kit(%L, %L, %s, %s, %L);\n\n',
        k.kit, k.title,
        CASE WHEN k.description IS NULL THEN 'NULL' ELSE quote_literal(k.description) END,
        CASE WHEN k.setup_sql IS NULL THEN 'NULL' ELSE rvbbit._kit_dq(k.setup_sql, 'ksetup') END,
        k.version);

    IF k.setup_sql IS NOT NULL THEN
        v_out := v_out || E'-- ── setup (kit-owned schemas/tables/views/roles) ──\n'
              || k.setup_sql || E'\n\n';
    END IF;

    SELECT string_agg(
        format(E'SELECT rvbbit.upsert_plate(%L, %L, %s, %s::jsonb, %s::jsonb, %s::jsonb, %L, %s, %s);',
            p.plate_id, p.title,
            rvbbit._kit_dq(p.template, 'ktpl'),
            rvbbit._kit_dq(p.queries::text, 'kq'),
            rvbbit._kit_dq(p.actions::text, 'ka'),
            rvbbit._kit_dq(p.params::text, 'kp'),
            p.kit,
            CASE WHEN p.description IS NULL THEN 'NULL' ELSE quote_literal(p.description) END,
            p.template_version),
        E'\n' ORDER BY p.plate_id)
    INTO v_plates
    FROM rvbbit.plates p WHERE p.kit = p_kit;

    SELECT string_agg(
        format('UPDATE rvbbit.plates SET module = %L WHERE plate_id = %L;', p.module, p.plate_id),
        E'\n' ORDER BY p.plate_id)
    INTO v_modules
    FROM rvbbit.plates p WHERE p.kit = p_kit AND p.module IS NOT NULL;

    SELECT string_agg(
        format('SELECT rvbbit.upsert_kit_contract(%L, %L, %L, %s, %s);',
            c.kit, c.module, c.contract_id,
            rvbbit._kit_dq(c.violations_sql, 'kv'),
            CASE WHEN c.description IS NULL THEN 'NULL' ELSE quote_literal(c.description) END),
        E'\n' ORDER BY c.module, c.contract_id)
    INTO v_contracts
    FROM rvbbit.kit_contracts c WHERE c.kit = p_kit;

    SELECT string_agg(
        format('SELECT rvbbit.upsert_kit_rule(%L, %L, %L, %s, %s::jsonb, %s, %s);',
            r.kit, r.rule_set, r.rule_id,
            rvbbit._kit_dq(r.when_sql, 'kw'),
            rvbbit._kit_dq(r.verdict::text, 'kvj'),
            r.priority,
            CASE WHEN r.description IS NULL THEN 'NULL' ELSE quote_literal(r.description) END),
        E'\n' ORDER BY r.rule_set, r.priority, r.rule_id)
    INTO v_rules
    FROM rvbbit.kit_rules r WHERE r.kit = p_kit AND r.active;

    SELECT string_agg(
        format('SELECT rvbbit.upsert_kit_rule_set(%L, %L, %s, %s);',
            rs.kit, rs.rule_set,
            rvbbit._kit_dq(rs.subject_sql, 'krs'),
            CASE WHEN rs.description IS NULL THEN 'NULL' ELSE quote_literal(rs.description) END),
        E'\n' ORDER BY rs.rule_set)
    INTO v_rule_sets
    FROM rvbbit.kit_rule_sets rs WHERE rs.kit = p_kit;

    SELECT string_agg(
        format(E'DELETE FROM rvbbit.operators WHERE name = %L;\nINSERT INTO rvbbit.operators SELECT * FROM jsonb_populate_record(NULL::rvbbit.operators, %s::jsonb);',
            o.name,
            rvbbit._kit_dq(to_jsonb(o)::text, 'kop')),
        E'\n' ORDER BY o.name)
    INTO v_ops
    FROM rvbbit.operators o WHERE o.kit = p_kit;

    v_out := v_out
        || E'-- ── plates ──\n' || coalesce(v_plates, '-- (none)') || E'\n\n'
        || E'-- ── module assignments ──\n' || coalesce(v_modules, '-- (none)') || E'\n\n'
        || E'-- ── contracts (empty result = green) ──\n' || coalesce(v_contracts, '-- (none)') || E'\n\n'
        || E'-- ── rules (decision tables; first match wins) ──\n' || coalesce(v_rules, '-- (none)') || E'\n\n'
        || E'-- ── rule sets (subject registrations for live observability) ──\n' || coalesce(v_rule_sets, '-- (none)') || E'\n\n'
        || E'-- ── operators (kit-scoped) ──\n' || coalesce(v_ops, '-- (none)') || E'\n\n'
        || E'-- ── metric_defs (reserved: definitions bound to kit canonical views) ──\n-- (none)\n\n'
        || E'-- ── cube_defs (reserved) ──\n-- (none)\n';

    RETURN v_out;
END
$ek$;
