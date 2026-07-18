-- Synthetic book of business for the crm kit — derived from Beacon Hill
-- Heating & Air's appointment customers so cross-kit joins are REAL
-- (scheduling.appointments.customer_id holds display names in v0; crm
-- keeps ids and joins on name until a domain kit tightens the weld).
-- Demo/bench only. Re-runnable.

BEGIN;

DELETE FROM crm.interactions WHERE interaction_id LIKE 'int-bhh-%';
DELETE FROM crm.customers WHERE customer_id LIKE 'cust-bhh-%';

-- Customers: everyone the shop has ever scheduled + a few pure leads.
INSERT INTO crm.customers (customer_id, name, phone, email, address, status, first_seen, last_seen)
SELECT 'cust-bhh-' || lpad(row_number() OVER (ORDER BY name)::text, 3, '0'),
       name,
       '617-555-0' || lpad((100 + row_number() OVER (ORDER BY name))::text, 3, '0'),
       lower(replace(name, ' ', '.')) || '@example.com',
       addr,
       status, first_seen, last_seen
FROM (
    SELECT ap.customer_id AS name,
           min(ap.address) AS addr,
           min(ap.starts_at) - interval '90 days' AS first_seen,
           max(ap.starts_at) AS last_seen,
           CASE
             WHEN max(ap.starts_at) < now() - interval '10 days' THEN 'lapsed'
             ELSE 'active'
           END AS status
    FROM scheduling.appointments ap
    WHERE ap.appt_id LIKE 'appt-bhh-%' OR ap.appt_id LIKE 'appt-%'
    GROUP BY ap.customer_id
) src;

INSERT INTO crm.customers (customer_id, name, phone, email, address, status, first_seen, last_seen) VALUES
    ('cust-bhh-l01', 'Vera Almeida',  '617-555-0201', 'vera.almeida@example.com',  '3 Acorn St',      'lead', now() - interval '5 days',  now() - interval '2 days'),
    ('cust-bhh-l02', 'Owen McBride',  '617-555-0202', 'owen.mcbride@example.com',  '55 W Cedar St',   'lead', now() - interval '40 days', now() - interval '35 days'),
    ('cust-bhh-l03', 'Priya Shah',    '617-555-0203', 'priya.shah@example.com',    '18 Louisburg Sq', 'lead', now() - interval '3 days',  now() - interval '1 day');

-- Interactions: calls/texts/emails/notes only — completed JOBS reach
-- crm.v_interactions through the cross-kit fitting (union over
-- scheduling), so they are never duplicated here.
SELECT setseed(0.37);
INSERT INTO crm.interactions (interaction_id, customer_id, at, channel, summary, outcome)
SELECT 'int-bhh-' || lpad(gs::text, 4, '0'),
       c.customer_id,
       now() - make_interval(days => floor(random() * 60)::int,
                             hours => floor(random() * 10)::int),
       (ARRAY['call','call','call','text','email','note'])[1 + floor(random() * 6)::int],
       (ARRAY['asked about seasonal maintenance plan',
              'rescheduling request',
              'quote follow-up',
              'billing question',
              'thermostat acting up again',
              'left voicemail re: annual service',
              'confirmed upcoming appointment',
              'asked for duct cleaning pricing'])[1 + floor(random() * 8)::int],
       (ARRAY[NULL, NULL, 'resolved', 'needs quote', 'callback scheduled'])[1 + floor(random() * 5)::int]
FROM generate_series(1, 70) gs
CROSS JOIN LATERAL (
    SELECT customer_id FROM crm.customers
    WHERE customer_id LIKE 'cust-bhh-%'
    ORDER BY md5(gs::text || customer_id) LIMIT 1
) c;

-- Recent touches for the hot-lead rule: Vera and Priya called this week.
INSERT INTO crm.interactions (interaction_id, customer_id, at, channel, summary, outcome) VALUES
    ('int-bhh-x001', 'cust-bhh-l01', now() - interval '1 day',  'call', 'wants mini-split install quote', 'site visit proposed'),
    ('int-bhh-x003', 'cust-bhh-l03', now() - interval '6 hours', 'text', 'send over the maintenance plan pricing', NULL);

COMMIT;
