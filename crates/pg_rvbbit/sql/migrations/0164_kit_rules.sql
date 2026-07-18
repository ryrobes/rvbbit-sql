-- 0164: kit_rules — decision tables as rows (KIT_PLATES_PLAN §6, tier 2).
--
-- A rule set is a priority-ordered decision table: each rule is a boolean SQL
-- EXPRESSION over a jsonb `subject` plus the verdict it decides. Evaluation is
-- first-match-wins, the winning rule_id rides with the verdict (provenance),
-- and a BROKEN rule fails loudly in-band — it wins with an error verdict
-- rather than silently falling through to a wrong answer. Plates consume
-- verdicts set-based via LATERAL; plates trigger, rules decide, never the
-- template.

CREATE TABLE IF NOT EXISTS rvbbit.kit_rules (
    kit         text NOT NULL,
    rule_set    text NOT NULL,
    rule_id     text NOT NULL,
    priority    int  NOT NULL DEFAULT 100,   -- lower evaluates first
    when_sql    text NOT NULL,               -- boolean expression over `subject` jsonb
    verdict     jsonb NOT NULL,              -- what this rule decides
    description text,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (kit, rule_set, rule_id)
);

CREATE OR REPLACE FUNCTION rvbbit.upsert_kit_rule(
    p_kit text,
    p_rule_set text,
    p_rule_id text,
    p_when_sql text,
    p_verdict jsonb,
    p_priority int DEFAULT 100,
    p_description text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $ukr$
BEGIN
    -- when_sql is an EXPRESSION, not a statement — no smuggling.
    IF p_when_sql ~ ';' THEN
        RAISE EXCEPTION 'rule % when_sql must be a single boolean expression (no semicolons)', p_rule_id;
    END IF;
    INSERT INTO rvbbit.kit_rules (kit, rule_set, rule_id, priority, when_sql, verdict, description)
    VALUES (p_kit, p_rule_set, p_rule_id, p_priority, p_when_sql, p_verdict, p_description)
    ON CONFLICT (kit, rule_set, rule_id) DO UPDATE SET
        priority = EXCLUDED.priority,
        when_sql = EXCLUDED.when_sql,
        verdict = EXCLUDED.verdict,
        description = EXCLUDED.description,
        active = true,
        updated_at = clock_timestamp();
END
$ukr$;

-- First matching rule's verdict for one subject. Zero rows = no rule matched
-- (rule sets wanting a default add a priority-999 rule with when_sql 'true').
CREATE OR REPLACE FUNCTION rvbbit.rule_verdict(
    p_kit text,
    p_rule_set text,
    p_subject jsonb
) RETURNS TABLE (rule_id text, verdict jsonb)
LANGUAGE plpgsql STABLE
AS $rv$
DECLARE
    r record;
    v_hit boolean;
BEGIN
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
            rule_id := r.rule_id;
            verdict := jsonb_build_object('rule_error', true, 'error', SQLERRM);
            RETURN NEXT;
            RETURN;
        END;
        IF v_hit THEN
            rule_id := r.rule_id;
            verdict := r.verdict;
            RETURN NEXT;
            RETURN;
        END IF;
    END LOOP;
END
$rv$;
