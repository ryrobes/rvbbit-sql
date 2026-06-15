-- 0034_brain_acl_management — assign roles to docs + an admin (unfiltered) listing.
--
-- Default-deny means a freshly-ingested doc with NO role is invisible to everyone — including inside
-- the ACL-filtered explorer (brain_tree) — so there was no way to find it and grant access without
-- raw SQL. brain_all_docs() is the admin/unfiltered listing (every doc + its roles + an unassigned
-- flag) for triage; brain_set_doc_roles() replaces a doc's role set; brain_list_roles() backs the
-- role pickers. Additive + idempotent. (brain_grant/brain_revoke already manage role→email.)

-- replace a document's allowed-role set (registers unknown roles; '' entries dropped)
CREATE OR REPLACE FUNCTION rvbbit.brain_set_doc_roles(p_doc bigint, p_roles text[])
RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.brain_documents WHERE doc_id = p_doc) THEN
        RAISE EXCEPTION 'rvbbit.brain_set_doc_roles: doc % not found', p_doc;
    END IF;
    DELETE FROM rvbbit.brain_doc_roles WHERE doc_id = p_doc;
    INSERT INTO rvbbit.brain_roles (role)
        SELECT DISTINCT btrim(r) FROM unnest(coalesce(p_roles, '{}')) r WHERE btrim(r) <> ''
        ON CONFLICT DO NOTHING;
    INSERT INTO rvbbit.brain_doc_roles (doc_id, role)
        SELECT DISTINCT p_doc, btrim(r) FROM unnest(coalesce(p_roles, '{}')) r WHERE btrim(r) <> ''
        ON CONFLICT DO NOTHING;
END $fn$;

-- admin/unfiltered listing: EVERY doc, its roles, and whether it's unassigned (role-less = nobody
-- can see it). NOT access-controlled — the operator/triage surface, distinct from brain_tree(email).
CREATE OR REPLACE FUNCTION rvbbit.brain_all_docs()
RETURNS TABLE(folder_path text, doc_id bigint, title text, source text, mime text, author text,
              occurred_at timestamptz, ingested_at timestamptz, chunks bigint, roles text[], unassigned boolean)
LANGUAGE sql STABLE AS $fn$
    SELECT d.folder_path, d.doc_id, d.title, s.label, d.mime, d.author, d.occurred_at, d.ingested_at,
           (SELECT count(*) FROM rvbbit.brain_chunks c WHERE c.doc_id = d.doc_id) AS chunks,
           coalesce((SELECT array_agg(dr.role ORDER BY dr.role) FROM rvbbit.brain_doc_roles dr
                     WHERE dr.doc_id = d.doc_id), '{}') AS roles,
           NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_roles dr WHERE dr.doc_id = d.doc_id) AS unassigned
    FROM rvbbit.brain_documents d
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    ORDER BY d.folder_path, d.title;
$fn$;

-- known roles (anywhere) + their member/doc counts — for the role pickers + an access overview
CREATE OR REPLACE FUNCTION rvbbit.brain_list_roles()
RETURNS TABLE(role text, members bigint, docs bigint) LANGUAGE sql STABLE AS $fn$
    SELECT r.role,
           (SELECT count(*) FROM rvbbit.brain_role_members m WHERE m.role = r.role) AS members,
           (SELECT count(*) FROM rvbbit.brain_doc_roles dr WHERE dr.role = r.role) AS docs
    FROM (SELECT role FROM rvbbit.brain_roles
          UNION SELECT role FROM rvbbit.brain_doc_roles
          UNION SELECT role FROM rvbbit.brain_role_members) r
    ORDER BY r.role;
$fn$;

-- members (principals) of a set of roles — for "who can see this" in the doc viewer
CREATE OR REPLACE FUNCTION rvbbit.brain_role_member_list(p_roles text[])
RETURNS TABLE(role text, principal text) LANGUAGE sql STABLE AS $fn$
    SELECT role, principal FROM rvbbit.brain_role_members
    WHERE role = ANY(coalesce(p_roles, '{}')) ORDER BY role, principal;
$fn$;
