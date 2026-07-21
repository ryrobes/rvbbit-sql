-- certify — the zero-UI example kit (docs/KIT_PLATES_PLAN.md, 0204 round)
--
-- A minimal intake→extract→validate→document workflow: field techs text
-- photos to the agent, the agent files them into cases, the LOGIC PLATE
-- says what's still missing, and the office plate is just the reporting
-- surface. This file is an EXAMPLE (idempotent, safe to re-run), not a
-- migration — it's also the reference answer to "how do chat images end
-- up in a kit": the agent maps conversation ("this is for the Henderson
-- job") onto the kit's nouns via the actions below; the logic plate's
-- explanation is the agent's instruction sheet.

CREATE SCHEMA IF NOT EXISTS certify;

CREATE TABLE IF NOT EXISTS certify.cases (
    case_id   serial PRIMARY KEY,
    customer  text NOT NULL,
    site      text,
    county    text NOT NULL DEFAULT 'Alachua',
    status    text NOT NULL DEFAULT 'collecting',   -- collecting | ready | filed
    opened_at timestamptz NOT NULL DEFAULT now(),
    filed_at  timestamptz,
    pdf_path  text
);
COMMENT ON TABLE certify.cases IS 'One certification case per install/inspection. status: collecting -> ready -> filed.';

CREATE TABLE IF NOT EXISTS certify.artifacts (
    artifact_id serial PRIMARY KEY,
    case_id     int NOT NULL REFERENCES certify.cases(case_id),
    kind        text NOT NULL CHECK (kind IN ('nameplate', 'backplate', 'tags', 'notes')),
    path        text,
    fields      jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence  numeric,
    added_at    timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE certify.artifacts IS 'Filed evidence per case. kind is the intake vocabulary the agent maps photos onto; fields/confidence come from extract_image.';

SELECT rvbbit.upsert_kit('certify', 'Certification Intake (example)',
  'Zero-UI intake example: photos arrive via chat, the agent files them with file_artifact, kit_pulse says what is missing, render_pdf files the certificate. The logic plate is the agent''s entire interface.');

-- ── the logic plate: checks + explanation + the agent's write API ────────
SELECT rvbbit.upsert_plate(
  'certify/intake-rules',
  'Certification intake rules (logic)',
  $tpl$A case is fileable when it holds all three photos — nameplate, backplate, tags — and every extracted field reads at confidence 0.75 or better. Cases older than 7 days that are still collecting need a nudge to the tech.

AGENT INSTRUCTIONS: when photos arrive in chat, ask which job they belong to if unclear, save them under /staging/certify/, call extract_image on nameplate shots (fields: model, serial, btu, seer), then file each with the file_artifact action (kind = nameplate | backplate | tags | notes). If a field's confidence is below 0.75, ask the tech to re-shoot before they leave the site. When every check below is green for a case, render the county PDF (render_pdf) and mark_filed.$tpl$,
  jsonb_build_object(
    'missing_photos', jsonb_build_object('sql', $q$
      SELECT c.case_id, c.customer, k.kind AS missing
      FROM certify.cases c
      CROSS JOIN (VALUES ('nameplate'), ('backplate'), ('tags')) AS k(kind)
      WHERE c.status = 'collecting'
        AND NOT EXISTS (SELECT 1 FROM certify.artifacts a
                        WHERE a.case_id = c.case_id AND a.kind = k.kind)
      ORDER BY c.case_id, k.kind
    $q$),
    'low_confidence', jsonb_build_object('sql', $q$
      SELECT c.case_id, c.customer, a.kind, a.confidence,
             (SELECT string_agg(key, ', ') FROM jsonb_each_text(a.fields)
              WHERE value IS NULL) AS unreadable_fields
      FROM certify.artifacts a JOIN certify.cases c USING (case_id)
      WHERE c.status = 'collecting' AND a.confidence IS NOT NULL AND a.confidence < 0.75
    $q$),
    'stale_cases', jsonb_build_object('sql', $q$
      SELECT case_id, customer, opened_at
      FROM certify.cases
      WHERE status = 'collecting' AND opened_at < now() - interval '7 days'
    $q$)
  ),
  jsonb_build_object(
    'open_case', jsonb_build_object(
      'sql', 'INSERT INTO certify.cases (customer, site, county) VALUES ({{customer}}, {{site}}, {{county}})',
      'args', jsonb_build_array(
        jsonb_build_object('name', 'customer', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'site', 'type', 'text', 'required', false),
        jsonb_build_object('name', 'county', 'type', 'text', 'required', false)),
      'description', 'Open a certification case for a customer/site'),
    'file_artifact', jsonb_build_object(
      'sql', $q$INSERT INTO certify.artifacts (case_id, kind, path, fields, confidence)
              VALUES (nullif({{case_id}},'')::int, {{kind}}, {{path}}, coalesce(nullif({{fields}},''),'{}')::jsonb, nullif({{confidence}},'')::numeric)$q$,
      'args', jsonb_build_array(
        jsonb_build_object('name', 'case_id', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'kind', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'path', 'type', 'text', 'required', false),
        jsonb_build_object('name', 'fields', 'type', 'text', 'required', false),
        jsonb_build_object('name', 'confidence', 'type', 'text', 'required', false)),
      'description', 'File a photo/notes artifact against a case (the agent''s intake verb)'),
    'mark_filed', jsonb_build_object(
      'sql', $q$UPDATE certify.cases SET status = 'filed', filed_at = now(), pdf_path = {{pdf_path}}
              WHERE case_id = nullif({{case_id}},'')::int$q$,
      'args', jsonb_build_array(
        jsonb_build_object('name', 'case_id', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'pdf_path', 'type', 'text', 'required', true)),
      'description', 'Close a case once the county PDF is rendered and delivered')
  ),
  '[]'::jsonb, 'certify', 'The agent-facing half of the certify kit: checks, instructions, and the write API.');
UPDATE rvbbit.plates SET surface = 'logic' WHERE plate_id = 'certify/intake-rules';

-- ── the reporting surface (what the office looks at) ─────────────────────
SELECT rvbbit.upsert_plate(
  'certify/cases',
  'Certification cases',
  $tpl$
<div style="display:flex; flex-direction:column; gap:12px; padding:16px 18px">
  <h2 style="margin:0; font-size:16px; font-weight:700">Certification cases</h2>
  <div rv-each="cases" style="display:flex; align-items:center; gap:10px; padding:9px 12px; border-radius:9px; background:rgba(128,128,128,0.07); border:1px solid rgba(128,128,128,0.18)">
    <span style="font-size:9px; text-transform:uppercase; letter-spacing:0.08em; padding:2px 8px; border-radius:99px; background:{{ row.chip_bg }}">{{ row.status }}</span>
    <div style="flex:1">
      <div style="font-size:13px; font-weight:600">{{ row.customer }} · {{ row.county }}</div>
      <div class="plate-muted" style="font-size:11px">{{ row.evidence }} · opened {{ row.opened }}</div>
    </div>
    <span rv-if="row.gaps" style="font-size:11px; color:rgba(220,90,70,0.95)">{{ row.gaps }} missing</span>
  </div>
</div>
$tpl$,
  jsonb_build_object('cases', jsonb_build_object('sql', $q$
    SELECT c.case_id, c.customer, c.county, c.status,
           to_char(c.opened_at, 'Mon DD') AS opened,
           count(a.artifact_id) || ' artifacts' AS evidence,
           (SELECT count(*) FROM (VALUES ('nameplate'),('backplate'),('tags')) k(kind)
            WHERE c.status = 'collecting'
              AND NOT EXISTS (SELECT 1 FROM certify.artifacts x
                              WHERE x.case_id = c.case_id AND x.kind = k.kind)) AS gaps,
           CASE c.status WHEN 'filed' THEN 'rgba(90,180,90,0.25)'
                         WHEN 'ready' THEN 'rgba(245,180,70,0.25)'
                         ELSE 'rgba(128,128,128,0.20)' END AS chip_bg
    FROM certify.cases c LEFT JOIN certify.artifacts a USING (case_id)
    GROUP BY c.case_id ORDER BY c.status = 'filed', c.opened_at DESC
  $q$)),
  '{}'::jsonb, '[]'::jsonb, 'certify', 'The office view: every case, its evidence, and what is still missing.');

-- ── demo seeds (three cases in three states) ─────────────────────────────
INSERT INTO certify.cases (customer, site, county, status, opened_at)
SELECT 'Henderson Residence', '412 Palm Ave', 'Alachua', 'collecting', now() - interval '2 days'
WHERE NOT EXISTS (SELECT 1 FROM certify.cases WHERE customer = 'Henderson Residence');
INSERT INTO certify.cases (customer, site, county, status, opened_at)
SELECT 'Bayfront Dental', '9 Harbor Rd', 'Broward', 'collecting', now() - interval '9 days'
WHERE NOT EXISTS (SELECT 1 FROM certify.cases WHERE customer = 'Bayfront Dental');
INSERT INTO certify.cases (customer, site, county, status, opened_at, filed_at, pdf_path)
SELECT 'Quake Episode HQ', '1 START St', 'DoomQL', 'filed', now() - interval '20 days', now() - interval '18 days', '/pdfs/doomql-cert-e1.pdf'
WHERE NOT EXISTS (SELECT 1 FROM certify.cases WHERE customer = 'Quake Episode HQ');

INSERT INTO certify.artifacts (case_id, kind, path, fields, confidence)
SELECT c.case_id, 'nameplate', '/staging/certify/henderson-nameplate.jpg',
       '{"model": "TRN-XR16", "serial": null, "btu": "36000", "seer": "16.2"}'::jsonb, 0.55
FROM certify.cases c WHERE c.customer = 'Henderson Residence'
  AND NOT EXISTS (SELECT 1 FROM certify.artifacts a WHERE a.case_id = c.case_id AND a.kind = 'nameplate');
INSERT INTO certify.artifacts (case_id, kind, path, fields, confidence)
SELECT c.case_id, 'tags', '/staging/certify/henderson-tags.jpg', '{"permit": "AL-2026-8841"}'::jsonb, 0.97
FROM certify.cases c WHERE c.customer = 'Henderson Residence'
  AND NOT EXISTS (SELECT 1 FROM certify.artifacts a WHERE a.case_id = c.case_id AND a.kind = 'tags');
