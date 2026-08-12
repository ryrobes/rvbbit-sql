CREATE ROLE mirror_reader LOGIN PASSWORD 'mirror_readonly';

CREATE SCHEMA sales;
CREATE SCHEMA support;

CREATE TABLE sales.customers (
    customer_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_ref text UNIQUE,
    display_name text NOT NULL,
    email text,
    region text,
    account_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sales.orders (
    order_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES sales.customers(customer_id),
    order_number text NOT NULL UNIQUE,
    status text NOT NULL,
    subtotal numeric(14,2),
    tax numeric(14,2),
    sales_rep text,
    placed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE support.tickets (
    ticket_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint REFERENCES sales.customers(customer_id),
    source text,
    priority text,
    subject text,
    tags text[],
    satisfaction_score integer,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO sales.customers
    (external_ref, display_name, email, region, account_metadata, created_at, updated_at)
VALUES
    ('C-001', 'Acme & Sons', 'billing@acme.example', 'Northeast',
     '{"segment":"enterprise","renewal_month":11}', '2025-10-03 14:15:00+00', '2026-08-10 09:00:00+00'),
    ('C-002', 'Northwind Field Ops', NULL, 'midwest ',
     '{"segment":"SMB","source":"partner"}', '2026-01-12 17:42:00+00', '2026-08-09 18:21:00+00'),
    ('C-003', 'Muller Industrial', 'ops@muller.example', 'EMEA',
     '{"segment":null,"notes":"uses PO numbers"}', '2026-03-28 08:05:00+00', '2026-08-11 11:02:00+00');

INSERT INTO sales.orders
    (customer_id, order_number, status, subtotal, tax, sales_rep, placed_at, updated_at)
VALUES
    (1, 'SO-10001', 'paid', 12500.00, 825.00, 'A. Rivera', '2026-07-01 12:05:00+00', '2026-07-02 09:00:00+00'),
    (1, 'SO-10002', 'partially_refunded', 419.95, -12.50, 'A. Rivera', '2026-08-02 19:44:00+00', '2026-08-10 16:30:00+00'),
    (2, 'SO-10003', 'pending', NULL, NULL, NULL, NULL, '2026-08-11 12:01:00+00');

INSERT INTO support.tickets
    (customer_id, source, priority, subject, tags, satisfaction_score, opened_at, closed_at, updated_at)
VALUES
    (1, 'email', 'high', 'Invoice total does not match PO', ARRAY['billing','renewal'], 4,
     '2026-08-01 13:00:00+00', '2026-08-01 16:12:00+00', '2026-08-01 16:12:00+00'),
    (2, 'chat', 'urgent', 'Crew cannot access mobile dashboard', ARRAY['login','mobile'], NULL,
     '2026-08-10 22:18:00+00', NULL, '2026-08-11 12:04:00+00'),
    (NULL, 'phone', 'low', 'anonymous pre-sales question', NULL, NULL,
     '2026-08-11 08:31:00+00', NULL, '2026-08-11 08:31:00+00');

GRANT CONNECT ON DATABASE commerce TO mirror_reader;
GRANT USAGE ON SCHEMA sales, support TO mirror_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales, support TO mirror_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA sales, support GRANT SELECT ON TABLES TO mirror_reader;
