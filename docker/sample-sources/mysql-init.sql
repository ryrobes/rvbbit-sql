CREATE USER IF NOT EXISTS 'mirror_reader'@'%' IDENTIFIED WITH mysql_native_password BY 'mirror_readonly';

CREATE TABLE contacts (
    contact_id bigint unsigned NOT NULL AUTO_INCREMENT PRIMARY KEY,
    account_code varchar(32),
    full_name varchar(200) NOT NULL,
    email varchar(320),
    lifecycle_stage enum('lead','qualified','customer','churned') NOT NULL,
    lead_score decimal(6,2),
    attributes json,
    created_at datetime(6) NOT NULL,
    updated_at timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
);

CREATE TABLE activities (
    activity_id bigint unsigned NOT NULL AUTO_INCREMENT PRIMARY KEY,
    contact_id bigint unsigned,
    activity_type varchar(40) NOT NULL,
    occurred_at datetime(6),
    duration_seconds integer,
    outcome varchar(120),
    raw_payload text,
    updated_at timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_activity_contact FOREIGN KEY (contact_id) REFERENCES contacts(contact_id)
);

INSERT INTO contacts
    (account_code, full_name, email, lifecycle_stage, lead_score, attributes, created_at)
VALUES
    ('A-100', 'Jamie O''Neil', 'jamie@example.test', 'customer', 98.50,
     JSON_OBJECT('campaign', 'spring-2026', 'consent', true), '2026-02-11 09:14:22.120000'),
    ('A-101', 'Sam Example', '', 'qualified', 61.25,
     JSON_OBJECT('campaign', NULL, 'preferred_channel', 'SMS'), '2026-06-30 23:59:59.999999'),
    (NULL, 'Duplicate-ish Lead', 'JAMIE@example.test ', 'lead', NULL,
     JSON_OBJECT('import_batch', 17), '2026-08-11 10:00:00.000000');

INSERT INTO activities
    (contact_id, activity_type, occurred_at, duration_seconds, outcome, raw_payload)
VALUES
    (1, 'meeting', '2026-08-01 15:30:00.000000', 2700, 'follow_up', '{"attendees":3}'),
    (1, 'email_open', '2026-08-03 08:12:04.221000', NULL, NULL, 'pixel'),
    (2, 'call', NULL, -1, 'bad source timestamp', 'legacy import'),
    (NULL, 'web_visit', '2026-08-11 11:59:59.000000', 0, 'anonymous', NULL);

GRANT SELECT ON crm.* TO 'mirror_reader'@'%';
FLUSH PRIVILEGES;
