-- Managed Python node demo.
--
-- Start the runtime first:
--   docker compose -f docker/docker-compose.yml \
--                  -f docker/docker-compose.sidecars.yml up -d python-runtime
--
-- Then run this file in the bench database. It shows the intended shape:
-- SQL owns reference data, Python is deterministic workflow glue, and the
-- result is a normal cached/audited rvbbit operator call.

SELECT rvbbit.create_python_env(
    env_name => 'ops_rules',
    python_version => '3.12',
    requirements => ARRAY[]::text[],
    timeout_ms => 1000
);

SELECT rvbbit.create_python_handler(
    handler_name => 'ticket_sla_score',
    env_name => 'ops_rules',
    code => $py$
import re

def run(inputs):
    body = str(inputs.get("body") or "")
    tier = str(inputs.get("tier") or "standard").lower()
    arr = float(inputs.get("annual_revenue") or 0)
    open_tickets = int(inputs.get("open_tickets") or 0)
    score = 0.0
    flags = []
    if tier in {"enterprise", "strategic"}:
        score += 0.35
        flags.append("high_value_account")
    if arr >= 1000000:
        score += 0.25
        flags.append("revenue_risk")
    if open_tickets >= 3:
        score += 0.20
        flags.append("repeat_contact")
    if re.search(r"\b(outage|down|cannot access|checkout)\b", body, re.I):
        score += 0.35
        flags.append("possible_outage")
    return {
        "priority": "urgent" if score >= 0.70 else "elevated" if score >= 0.35 else "standard",
        "score": round(min(score, 1.0), 3),
        "flags": flags
    }
$py$,
    description => 'Deterministic SLA policy scoring for support tickets.'
);

DROP TABLE IF EXISTS demo_customers;
CREATE TABLE demo_customers (
    id int PRIMARY KEY,
    tier text NOT NULL,
    annual_revenue float8 NOT NULL
);
INSERT INTO demo_customers VALUES
    (101, 'enterprise', 2400000),
    (202, 'standard', 12000);

SELECT rvbbit.create_operator(
    op_name => 'ticket_sla',
    op_arg_names => ARRAY['customer_id', 'body', 'open_tickets'],
    op_return_type => 'jsonb',
    op_steps => jsonb_build_array(
        jsonb_build_object(
            'name', 'customer',
            'kind', 'sql',
            'sql', 'SELECT tier, annual_revenue FROM demo_customers WHERE id = $1::int',
            'params', jsonb_build_array('{{ inputs.customer_id }}')
        ),
        jsonb_build_object(
            'name', 'score',
            'kind', 'python',
            'env', 'ops_rules',
            'handler', 'ticket_sla_score',
            'inputs', jsonb_build_object(
                'body', '{{ inputs.body }}',
                'open_tickets', '{{ inputs.open_tickets }}',
                'tier', '{{ steps.customer.output.tier }}',
                'annual_revenue', '{{ steps.customer.output.annual_revenue }}'
            )
        )
    )
);

SELECT rvbbit.set_operator_wards('ticket_sla', jsonb_build_object(
    'post', jsonb_build_array(jsonb_build_object(
        'validator', jsonb_build_object(
            'sql', '($output::jsonb ? ''priority'') AND (($output::jsonb->>''priority'') IN (''standard'',''elevated'',''urgent''))'),
        'mode', 'blocking'))));

SELECT rvbbit.ticket_sla(
    '101',
    'Checkout is down and our team cannot access invoices',
    '4'
) AS scored_ticket;

SELECT operator, sub_calls
FROM rvbbit.receipts
WHERE operator = 'ticket_sla'
ORDER BY invocation_at DESC
LIMIT 1;
