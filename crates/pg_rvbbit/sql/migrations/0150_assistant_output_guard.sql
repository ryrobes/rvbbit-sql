-- 0150_assistant_output_guard.sql
-- A tool-calling agent used to stop at the transport's hidden 4k completion
-- cap even when its operator requested more. The engine now reports that stop
-- as output_truncated; never surface its incomplete command envelope as chat.

UPDATE rvbbit.operators
   SET steps = jsonb_set(
       steps,
       '{1,sql}',
       to_jsonb($result_sql$
SELECT jsonb_build_object(
           'reply',
           CASE
             WHEN $3 = 'output_truncated' THEN
               'I ran out of output space while building that, so I left the desktop unchanged. Try asking me to split it into smaller blocks.'
             WHEN _p.parsed IS NULL AND left(ltrim(coalesce($1, '')), 1) = '{' THEN
               'I could not finish a valid desktop command, so I left the desktop unchanged. Please try that again.'
             ELSE coalesce(_p.parsed->>'reply', nullif(trim($1), ''))
           END,
           'commands',
           CASE
             WHEN $3 = 'output_truncated'
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
           END
       ) AS result
  FROM (SELECT rvbbit.desktop_try_jsonb($1) AS parsed) _p
$result_sql$::text),
       true
   )
 WHERE name = 'desktop_assistant_turn'
   AND jsonb_typeof(steps) = 'array'
   AND jsonb_array_length(steps) > 1;
