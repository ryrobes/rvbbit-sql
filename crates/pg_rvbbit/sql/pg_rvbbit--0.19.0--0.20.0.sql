-- pg_rvbbit 0.19.0 -> 0.20.0
-- Loop 19: three flow-feature built-in operators. Each is a useful
-- general-purpose operator AND showcases one semantic-flow feature:
--   clean_year — retry-validated 4-digit year extraction
--   redact     — PII stripping with a blocking no-email post-ward
--   headline   — 3 takes + an LLM evaluator picks the punchiest

DO $$
BEGIN

PERFORM rvbbit.create_operator(
    op_name => 'clean_year',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text',
    op_system =>
        'You extract the calendar year an event took place from messy ' ||
        'text. Respond with ONLY a 4-digit year such as 1997. Expand ' ||
        'two-digit years (97 becomes 1997, 05 becomes 2005). If the ' ||
        'text states no year at all, respond with exactly: unknown',
    op_user => E'TEXT: {{ text }}\n\nYear:',
    op_max_tokens => 12,
    op_temperature => 0.0,
    op_description => 'Extract a clean 4-digit year from messy text (retry-validated).',
    op_parser => 'strip'
);
PERFORM rvbbit.set_operator_retry('clean_year',
    $cfg${"until":{"sql":"btrim($output) ~ '^((1[6-9]|20)[0-9]{2}|unknown)$'"},"max_attempts":3,"instructions":"Your previous answer was not a valid year. Respond with ONLY a 4-digit year such as 1997, or exactly the word: unknown"}$cfg$::jsonb);

PERFORM rvbbit.create_operator(
    op_name => 'redact',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text',
    op_system =>
        'You remove personally identifying information from text. ' ||
        'Replace each person name with [NAME], email address with ' ||
        '[EMAIL], phone number with [PHONE], street address with ' ||
        '[ADDRESS], and government id number with [ID]. Leave place ' ||
        'names such as cities, counties and states intact. Return ONLY ' ||
        'the rewritten text, preserving all other wording.',
    op_user => E'TEXT: {{ text }}\n\nRedacted text:',
    op_max_tokens => 1024,
    op_temperature => 0.0,
    op_description => 'Strip PII from text; post-ward rejects output that still contains an email.',
    op_parser => 'strip'
);
PERFORM rvbbit.set_operator_wards('redact',
    $cfg${"post":[{"validator":{"sql":"$output !~ '[A-Za-z0-9._%+-]+@[A-Za-z0-9._-]+'"},"mode":"blocking"}]}$cfg$::jsonb);

PERFORM rvbbit.create_operator(
    op_name => 'headline',
    op_shape => 'scalar',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text',
    op_system =>
        'You write one short, punchy headline that captures the single ' ||
        'most striking thing in the TEXT. Under 12 words. No quotation ' ||
        'marks, no trailing period. Return ONLY the headline.',
    op_user => E'TEXT: {{ text }}\n\nHeadline:',
    op_max_tokens => 32,
    op_temperature => 0.8,
    op_description => 'Generate a punchy headline; 3 takes, an LLM evaluator picks the best.',
    op_parser => 'strip'
);
PERFORM rvbbit.set_operator_takes('headline',
    $cfg${"factor":3,"reduce":"evaluator","evaluator":{"instructions":"Pick the headline that is the most vivid and specific while staying accurate to the text. Reply with only its number."}}$cfg$::jsonb);

END
$$;
