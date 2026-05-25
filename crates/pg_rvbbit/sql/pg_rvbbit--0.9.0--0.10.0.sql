-- pg_rvbbit 0.9.0 -> 0.10.0
-- Loop 6 / RYR-303 Tier A: pre-registered LLM operator bundle inspired
-- by Lars cascades. Pure DDL — no new Rust. Each row in
-- rvbbit.operators auto-generates a SQL wrapper function via the same
-- machinery used for the built-in means / about / summarize.

-- Drop any prior wrapper functions whose signatures might conflict with
-- the create_operator regeneration below (CREATE OR REPLACE FUNCTION
-- can't change return type). Safe even on fresh installs — IF EXISTS.
DROP FUNCTION IF EXISTS rvbbit.classify(text, text, jsonb);
DROP FUNCTION IF EXISTS rvbbit.extract(text, text, jsonb);
DROP FUNCTION IF EXISTS rvbbit.condense(text, jsonb);
DROP FUNCTION IF EXISTS rvbbit.sentiment(text, jsonb);
DROP FUNCTION IF EXISTS rvbbit.contradicts(text, text, jsonb);
DROP FUNCTION IF EXISTS rvbbit.supports(text, text, jsonb);
DROP FUNCTION IF EXISTS rvbbit.implies(text, text, jsonb);
DELETE FROM rvbbit.operators WHERE name IN
    ('classify','extract','condense','sentiment','contradicts','supports','implies');

DO $seed$
BEGIN

-- classify(text, categories) -> text
-- Argmax over a comma-separated category list. Returns ONLY the
-- best-matching category name.
PERFORM rvbbit.create_operator(
    op_name => 'classify',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text', 'categories'],
    op_return_type => 'text',
    op_system =>
        'You are a strict classifier. Given a TEXT and a comma-separated ' ||
        'list of CATEGORIES, return ONLY the single category name that ' ||
        'best matches the TEXT. Use the exact spelling from the list. ' ||
        'No explanation, no quotes, just the category name.',
    op_user =>
        E'CATEGORIES: {{ categories }}\n\nTEXT: {{ text }}\n\nBest category:',
    op_max_tokens => 32,
    op_description => 'Classify text into ONE of the comma-separated CATEGORIES.',
    op_parser => 'strip'
);

-- extract(text, what) -> text
-- Pull a specific fact/entity out of unstructured text. Returns the
-- literal value or 'NULL' if not present.
PERFORM rvbbit.create_operator(
    op_name => 'extract',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text', 'what'],
    op_return_type => 'text',
    op_system =>
        'You are a precise information extractor. Given a TEXT and a ' ||
        'description WHAT of the value to find, return ONLY the literal ' ||
        'value from the text. If the value is not present, return ' ||
        'exactly: NULL. No explanation, no quotes, no surrounding text.',
    op_user =>
        E'TEXT: {{ text }}\n\nWHAT: {{ what }}\n\nExtracted value:',
    op_max_tokens => 64,
    op_description => 'Extract a specific value WHAT from TEXT (or NULL).',
    op_parser => 'strip'
);

-- condense(text) -> text
-- Per-row summary in 1-3 sentences. For aggregate summarization across
-- a collection, use the existing rvbbit.summarize.
PERFORM rvbbit.create_operator(
    op_name => 'condense',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text',
    op_system =>
        'You are a concise summarizer. Return a 1-3 sentence summary ' ||
        'of the TEXT. Preserve the most important facts, names, and ' ||
        'numbers. Use plain prose — no bullet points, no preamble like ' ||
        '"Here is a summary".',
    op_user =>
        E'TEXT: {{ text }}\n\nSummary:',
    op_max_tokens => 200,
    op_description => 'Condense TEXT into a 1-3 sentence summary (scalar, per-row).',
    op_parser => 'strip'
);

-- sentiment(text) -> text
-- Classify into one of: positive, negative, neutral, mixed.
PERFORM rvbbit.create_operator(
    op_name => 'sentiment',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text',
    op_system =>
        'You are a sentiment classifier. Return ONLY one of: ' ||
        'positive, negative, neutral, mixed. Use lowercase. No ' ||
        'explanation, no period, just the label.',
    op_user =>
        E'TEXT: {{ text }}\n\nSentiment:',
    op_max_tokens => 8,
    op_description => 'Sentiment label: positive | negative | neutral | mixed.',
    op_parser => 'strip'
);

-- contradicts(a, b) -> bool
-- True iff statement A logically contradicts statement B.
PERFORM rvbbit.create_operator(
    op_name => 'contradicts',
    op_shape => 'scalar',
    op_arg_names => ARRAY['a', 'b'],
    op_return_type => 'bool',
    op_system =>
        'You are a strict logical relation classifier. Given two ' ||
        'statements A and B, decide whether A directly CONTRADICTS B. ' ||
        'They contradict if both cannot be true at the same time. ' ||
        'Respond ONLY with YES or NO.',
    op_user =>
        E'A: {{ a }}\n\nB: {{ b }}\n\nDoes A contradict B?',
    op_max_tokens => 8,
    op_description => 'Does statement A contradict statement B?',
    op_parser => 'yes_no'
);

-- supports(a, b) -> bool
-- True iff A provides evidence supporting B.
PERFORM rvbbit.create_operator(
    op_name => 'supports',
    op_shape => 'scalar',
    op_arg_names => ARRAY['a', 'b'],
    op_return_type => 'bool',
    op_system =>
        'You are a strict logical relation classifier. Given two ' ||
        'statements A and B, decide whether A provides direct evidence ' ||
        'SUPPORTING B. Respond ONLY with YES or NO.',
    op_user =>
        E'A: {{ a }}\n\nB: {{ b }}\n\nDoes A support B?',
    op_max_tokens => 8,
    op_description => 'Does statement A support statement B?',
    op_parser => 'yes_no'
);

-- implies(a, b) -> bool
-- True iff A logically implies B.
PERFORM rvbbit.create_operator(
    op_name => 'implies',
    op_shape => 'scalar',
    op_arg_names => ARRAY['a', 'b'],
    op_return_type => 'bool',
    op_system =>
        'You are a strict logical relation classifier. Given two ' ||
        'statements A and B, decide whether A logically IMPLIES B ' ||
        '(if A is true, B must also be true). Respond ONLY with YES or NO.',
    op_user =>
        E'A: {{ a }}\n\nB: {{ b }}\n\nDoes A imply B?',
    op_max_tokens => 8,
    op_description => 'Does statement A logically imply statement B?',
    op_parser => 'yes_no'
);

END
$seed$;
