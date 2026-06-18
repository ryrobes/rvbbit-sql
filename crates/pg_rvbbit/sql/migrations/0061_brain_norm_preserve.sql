-- 0061_brain_norm_preserve — stop the Snowball stemmer from merging collision-prone named entities.
--
-- 0058's normalization plural-strips via the English Snowball stemmer, which is right for common nouns
-- (student/students) but wrong for proper nouns that merely END in 's': it stems `canvas`→`canva`,
-- colliding with `Canva` (the design tool) — so the Canvas LMS and Canva were keyed identically and
-- counted as one shared entity, asserting a relationship that isn't real. (campus/status/focus happen to
-- survive; canvas doesn't — the stemmer is heuristic, not a dictionary.)
--
-- Fix: a small "preserve literal" set (GUC rvbbit.brain_norm_preserve) of collision-prone names whose
-- normalized key is just the lowercased literal — no stemming, no state-aliasing. _brain_norm_key checks
-- it first. Then recompute any cached keys that drift under the new logic (canvas → canvas again).

-- Collision-prone tokens to key verbatim (lowercased). Tunable; extend as new collisions surface.
CREATE OR REPLACE FUNCTION rvbbit._brain_norm_preserve()
RETURNS text[] LANGUAGE sql STABLE AS $fn$
    SELECT string_to_array(
        coalesce(nullif(current_setting('rvbbit.brain_norm_preserve', true), ''),
            'canva,canvas'), ',');
$fn$;

-- Normalized matching key: preserve-set verbatim → state canon → per-word Snowball stem → lowercased.
CREATE OR REPLACE FUNCTION rvbbit._brain_norm_key(p_label text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT CASE
        WHEN lower(btrim(p_label)) = ANY (rvbbit._brain_norm_preserve())
            THEN lower(btrim(p_label))
        ELSE coalesce(
            rvbbit._brain_state_full(p_label),
            nullif((SELECT string_agg(coalesce((ts_lexize('english_stem', wrd))[1], wrd), ' ' ORDER BY ord)
                    FROM unnest(regexp_split_to_array(lower(btrim(p_label)), '\s+')) WITH ORDINALITY AS t(wrd, ord)), ''),
            lower(btrim(p_label)))
    END;
$fn$;

-- Recompute cached keys that drift under the new logic (one-time; covers Canvas → canvas).
UPDATE rvbbit.brain_node_norm bn
   SET nk = rvbbit._brain_norm_key(n.label)
  FROM rvbbit.kg_nodes n
 WHERE n.node_id = bn.node_id
   AND bn.nk IS DISTINCT FROM rvbbit._brain_norm_key(n.label);
