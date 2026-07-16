-- 0152_assistant_provider_failures.sql
-- Large structured assistant turns routinely spend 90-115s generating their
-- final HTML command. OpenRouter's old 120s backend timeout could expire while
-- reading that successful response body, after the agent had already completed
-- its SQL probes. Preserve such transport failures as a structured assistant
-- result instead of aborting the pipeline into SQL NULL / "(no reply)".

UPDATE rvbbit.backends
   SET timeout_ms = 300000
 WHERE name = 'openrouter'
   AND transport = 'openai_chat'
   AND timeout_ms <= 120000;

UPDATE rvbbit.operators
   SET steps = jsonb_set(
       jsonb_set(
         steps,
         '{0,continue_on_error}',
         'true'::jsonb,
         true
       ),
       '{0,budget,wall_ms}',
       '360000'::jsonb,
       true
   )
 WHERE name = 'desktop_assistant_turn'
   AND jsonb_typeof(steps) = 'array'
   AND jsonb_array_length(steps) > 1;

UPDATE rvbbit.operators
   SET steps = jsonb_set(
       jsonb_set(
         steps,
         '{1,sql}',
         to_jsonb($result_sql$
SELECT jsonb_build_object(
           'reply',
           CASE
             WHEN $3 IN ('provider_error', 'memory_error') THEN
               coalesce(
                 nullif(trim($1), ''),
                 'The assistant could not complete this turn: '
                   || coalesce(nullif(trim($4), ''), 'unknown provider failure')
               )
             WHEN $3 = 'output_truncated' THEN
               'I ran out of output space while building that, so I left the desktop unchanged. Try asking me to split it into smaller blocks.'
             WHEN _p.parsed IS NULL AND left(ltrim(coalesce($1, '')), 1) = '{' THEN
               'I could not finish a valid desktop command, so I left the desktop unchanged. Please try that again.'
             ELSE coalesce(_p.parsed->>'reply', nullif(trim($1), ''))
           END,
           'commands',
           CASE
             WHEN $3 IN ('provider_error', 'memory_error', 'output_truncated')
               OR (_p.parsed IS NULL AND left(ltrim(coalesce($1, '')), 1) = '{')
             THEN '[]'::jsonb
             ELSE coalesce(_p.parsed->'commands', '[]'::jsonb)
           END,
           'agent_run_id', $2,
           'status',
           CASE
             WHEN $3 = 'output_truncated' THEN 'output_truncated'
             WHEN _p.parsed IS NULL AND left(ltrim(coalesce($1, '')), 1) = '{'
               THEN 'invalid_structured_output'
             ELSE $3
           END,
           'error', nullif(trim($4), '')
       ) AS result
  FROM (SELECT rvbbit.desktop_try_jsonb($1) AS parsed) _p
$result_sql$::text),
         true
       ),
       '{1,params}',
       jsonb_build_array(
         '{{ steps.assistant.output }}',
         '{{ steps.assistant.agent_run_id }}',
         '{{ steps.assistant.status }}',
         '{{ steps.assistant.error }}'
       ),
       true
   )
 WHERE name = 'desktop_assistant_turn'
   AND jsonb_typeof(steps) = 'array'
   AND jsonb_array_length(steps) > 1;
