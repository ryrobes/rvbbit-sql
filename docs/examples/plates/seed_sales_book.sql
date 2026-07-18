-- Synthetic sales history for Beacon Hill Heating & Air — quotes over
-- ~120 days against the crm customer book, invoices for accepted work.
-- Deterministic; demo/bench only; re-runnable.

BEGIN;

DELETE FROM sales.invoices WHERE invoice_id LIKE 'inv-bhh-%';
DELETE FROM sales.quotes WHERE quote_id LIKE 'q-bhh-%';

SELECT setseed(0.51);
INSERT INTO sales.quotes (quote_id, customer_id, title, amount, status, created_at, decided_at)
SELECT q.quote_id, q.name, q.title, q.amount, q.status, q.created_at,
       CASE WHEN q.status IN ('accepted', 'declined')
            THEN q.created_at + make_interval(days => 2 + floor(random() * 12)::int)
            ELSE NULL END
FROM (
    SELECT 'q-bhh-' || lpad(gs::text, 4, '0') AS quote_id,
           c.name,
           (ARRAY['Furnace replacement', 'Mini-split install', 'AC condenser swap',
                  'Duct cleaning + reseal', 'Annual maintenance plan', 'Heat pump conversion',
                  'Thermostat + zoning upgrade', 'Boiler repair', 'Water heater install',
                  'Emergency compressor repair'])[1 + floor(random() * 10)::int]
             || ' — ' || split_part(c.name, ' ', 1) || '''s place' AS title,
           round((280 + random() * random() * 14000)::numeric, 0) AS amount,
           CASE
             WHEN gs % 25 = 0 THEN 'draft'
             WHEN gs % 25 <= 3 THEN 'expired'
             WHEN gs % 25 <= 9 THEN 'sent'
             WHEN gs % 25 <= 19 THEN 'accepted'
             ELSE 'declined'
           END AS status,
           now() - make_interval(days => floor(random() * 120)::int,
                                 hours => floor(random() * 9)::int) AS created_at
    FROM generate_series(1, 80) gs
    CROSS JOIN LATERAL (
        SELECT name FROM crm.customers WHERE customer_id LIKE 'cust-bhh-%'
        ORDER BY md5(gs::text || customer_id) LIMIT 1
    ) c
) q;

-- Recent sent quotes should skew recent (they are the live pipeline);
-- pull sent/draft quotes into the last 3 weeks so the board feels live.
UPDATE sales.quotes
SET created_at = now() - make_interval(days => (abs(hashtext(quote_id)) % 20)::int,
                                       hours => (abs(hashtext(quote_id)) % 9)::int)
WHERE quote_id LIKE 'q-bhh-%' AND status IN ('draft', 'sent')
  AND created_at < now() - interval '21 days';

-- Two planted stale sent quotes for the deal_watch rule.
UPDATE sales.quotes SET created_at = now() - interval '26 days'
WHERE quote_id IN (SELECT quote_id FROM sales.quotes
                   WHERE quote_id LIKE 'q-bhh-%' AND status = 'sent'
                   ORDER BY quote_id LIMIT 2);

-- Invoices: one per accepted quote (90%), aged realistically.
INSERT INTO sales.invoices (invoice_id, customer_id, quote_id, amount, status, issued_at, due_at, paid_at)
SELECT 'inv-bhh-' || lpad(row_number() OVER (ORDER BY q.quote_id)::text, 4, '0'),
       q.customer_id, q.quote_id, q.amount,
       CASE
         WHEN q.decided_at < now() - interval '45 days' THEN 'paid'
         WHEN q.decided_at < now() - interval '35 days' THEN 'overdue'
         ELSE 'sent'
       END,
       q.decided_at,
       q.decided_at + interval '30 days',
       CASE WHEN q.decided_at < now() - interval '45 days'
            THEN q.decided_at + make_interval(days => 8 + (abs(hashtext(q.quote_id)) % 22)::int)
            ELSE NULL END
FROM sales.quotes q
WHERE q.quote_id LIKE 'q-bhh-%' AND q.status = 'accepted'
  AND abs(hashtext(q.quote_id)) % 10 < 9;

COMMIT;
