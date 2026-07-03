-- 0120_html_block_turn_operator.sql
-- Agent-backed HTML Block turns for SQL Desktop. This is intentionally SQL-only:
-- it registers a semantic operator whose agent node can inspect live schema/data
-- through the built-in read-only query tool and returns a versioned HTML Block
-- artifact JSON document for Lens to apply as a revision.

SELECT rvbbit.create_operator(
    op_name        => 'html_block_turn',
    op_arg_names   => ARRAY['user_message', 'artifact_current', 'conversation_context', 'desktop_context'],
    op_arg_types   => ARRAY['text', 'jsonb', 'jsonb', 'jsonb'],
    op_return_type => 'jsonb',
    op_shape       => 'scalar',
    op_model       => 'openai/gpt-5.4-mini',
    op_max_tokens  => 8192,
    op_description => 'Agent turn for SQL Desktop HTML Blocks: returns {artifact:{title,html,queries,bindings},summary} as jsonb. Uses a bounded read-only query loop and records agent_messages receipts.',
    op_steps       => $steps$
[
  {
    "name": "designer",
    "kind": "agent",
    "model": "openai/gpt-5.4-mini",
    "system": "You are the SQL Desktop HTML Block designer. You create polished, self-contained HTML/CSS/JS apps backed by named SQL queries. The HTML is the primary artifact, not a built-in chart description. You may use the read-only query tool to inspect schema, sample rows, and validate query ideas. Return ONLY valid JSON. Do not wrap in markdown.\n\nReturn shape:\n{\n  \"artifact\": {\n    \"schemaVersion\": \"rvbbit.html_block.v1\",\n    \"title\": \"short title\",\n    \"html\": \"self-contained HTML fragment with style/script; call rvbbitQuery('query_id') or rvbbit.query('query_id') for data; call rvbbit.emitFilter({queryId, field, value, operator, targetQueryId}) for interactions\",\n    \"queries\": [\n      {\"id\":\"stable_snake_case\",\"title\":\"Human title\",\"role\":\"primary|detail|control|support\",\"sql\":\"SELECT ...\",\"filterable\":[\"field\"]}\n    ],\n    \"bindings\": [\n      {\"sourceQueryId\":\"query_id\",\"field\":\"field\",\"targetQueryId\":\"other_query_id\",\"targetField\":\"field\",\"operator\":\"eq|in|gte|lte\"}\n    ]\n  },\n  \"summary\": \"one short sentence\"\n}\n\nRules:\n- Keep HTML/CSS/JS self-contained. Do not use external scripts or network resources.\n- Prefer named queries over inline SQL. Arbitrary rvbbitQuery(sql) is allowed only for small runtime probes, not primary data dependencies.\n- Queries must be read-only SELECT/WITH statements. Do not emit DDL/DML.\n- Use stable query ids and preserve existing query ids unless the user asks for a structural change.\n- Preserve the current artifact when the user asks for a refinement; update only what is needed.\n- Make the UI polished and domain-specific, but compact enough for a desktop window.\n- Do not describe how to use the app inside the app UI.\n- If you need data, inspect it with the query tool first, then stop calling tools when you can return the artifact JSON.",
    "task": "User message:\n{{ inputs.user_message }}\n\nCurrent artifact JSON:\n{{ inputs.artifact_current }}\n\nRecent conversation JSON:\n{{ inputs.conversation_context }}\n\nDesktop context JSON:\n{{ inputs.desktop_context }}\n\nCreate the next HTML Block revision. Return ONLY the JSON object described in the system prompt.",
    "tools": [{ "builtin": "query" }],
    "max_iters": 8,
    "budget": { "cost_usd": 0.75, "wall_ms": 120000 },
    "tool_result_max_chars": 12000
  },
  {
    "name": "return_artifact",
    "kind": "sql",
    "sql": "SELECT jsonb_build_object('artifact', (($1)::jsonb)->'artifact', 'summary', (($1)::jsonb)->>'summary', 'agent_run_id', $2, 'status', $3) AS result",
    "params": [
      "{{ steps.designer.output }}",
      "{{ steps.designer.agent_run_id }}",
      "{{ steps.designer.status }}"
    ]
  }
]
$steps$
);

UPDATE rvbbit.operators
   SET cache_policy = 'never'
 WHERE name = 'html_block_turn';
