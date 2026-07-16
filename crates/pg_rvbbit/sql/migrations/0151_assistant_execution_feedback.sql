-- 0151_assistant_execution_feedback.sql
-- Lens now reports the latest real data-window run in
-- desktop_context.blocks[].execution. Teach the assistant that accepting a
-- create/update command is not proof that the block's asynchronous SQL ran.

UPDATE rvbbit.operators
   SET steps = jsonb_set(
       steps,
       '{0,system}',
       to_jsonb(
         (steps->0->>'system') || E'\n\nEXECUTION FEEDBACK\n'
         || E'- apply_report says whether a desktop command was accepted; it does NOT mean a block query succeeded.\n'
         || E'- Each data block may carry execution, the latest query state observed by the desktop: idle, running, done, or error. Treat it as the runtime truth for that block.\n'
         || E'- On execution.error, read the structured message/code/detail/hint and repair the SQL when the user asks about or continues work on that block. Never describe an errored block as working.\n'
         || E'- execution.done includes bounded columns, row counts, timing, and sample_rows. HTML app blocks may include per-query statements with query_id. Samples are intentionally small; use the query tool when you need more rows or a fresh validation.\n'
         || E'- If execution is idle or running, say so when relevant and do not invent results.'
       ),
       true
   )
 WHERE name = 'desktop_assistant_turn'
   AND jsonb_typeof(steps) = 'array'
   AND jsonb_array_length(steps) > 0
   AND coalesce(steps->0->>'system', '') NOT LIKE '%EXECUTION FEEDBACK%';
