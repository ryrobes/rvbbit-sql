-- 0146_desktop_assistant_turn_operator.sql
-- Desktop Assistant turns for the SQL Desktop (rvbbit-lens). SQL-only: registers
-- a semantic operator whose agent node inspects live schema/data through the
-- built-in read-only query tool and returns {reply, commands[]} — a
-- rvbbit.desktop_commands.v1 batch that the Lens client applies to the canvas
-- through its shell mutation API. The desktop verbs are deliberately NOT MCP
-- tools: they exist only in this contract and the Lens applier, so external MCP
-- clients can never see or call them (scoping by construction). Design:
-- rvbbit-lens/docs/DESKTOP_ASSISTANT_PLAN.md.

-- Lenient parse of the model's final message. A frontier model on a purely
-- conversational turn will sometimes answer in prose despite the contract;
-- that is a LEGITIMATE zero-command turn, not an error. Strip one markdown
-- fence if present, else NULL on anything that is not valid jsonb.
CREATE OR REPLACE FUNCTION rvbbit.desktop_try_jsonb(raw text)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $fn$
DECLARE
    cleaned text;
    embedded text;
BEGIN
    cleaned := trim(coalesce(raw, ''));
    cleaned := regexp_replace(cleaned, '^```[a-zA-Z]*\s*', '');
    cleaned := regexp_replace(cleaned, '\s*```$', '');
    BEGIN
        RETURN cleaned::jsonb;
    EXCEPTION WHEN others THEN
        NULL;
    END;
    -- Prose with an embedded JSON object (e.g. a sentence followed by a fenced
    -- block): try the outermost {...} span before giving up.
    embedded := substring(cleaned FROM '\{.*\}');
    IF embedded IS NOT NULL THEN
        BEGIN
            RETURN embedded::jsonb;
        EXCEPTION WHEN others THEN
            NULL;
        END;
    END IF;
    RETURN NULL;
END
$fn$;

DELETE FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';

SELECT rvbbit.create_operator(
    op_name        => 'desktop_assistant_turn',
    op_arg_names   => ARRAY['user_message', 'conversation_context', 'desktop_context'],
    op_arg_types   => ARRAY['text', 'jsonb', 'jsonb'],
    op_return_type => 'jsonb',
    op_shape       => 'scalar',
    op_model       => 'anthropic/claude-opus-4.8',
    op_max_tokens  => 16384,
    op_description => 'Desktop Assistant turn for the SQL Desktop: returns {reply, commands[]} as jsonb (rvbbit.desktop_commands.v1). Uses a generous read-only query loop; records agent_messages receipts.',
    op_steps       => $steps$
[
  {
    "name": "assistant",
    "kind": "agent",
    "model": "anthropic/claude-opus-4.8",
    "system": "You are the Datarabbit Desktop Assistant — the voice and hands of the user's SQL Desktop. The desktop is a canvas of blocks (SQL query blocks, charts, apps). You act by returning commands that materialize as blocks on the user's canvas; you speak in short, natural utterances.\n\nVOICE\n- 1–3 short sentences per reply. Conversational, specific, no essays, no markdown, no headers, no bullet lists in reply text.\n- You are part of the OS, not a chatbot in a window. Say what you did or found, plainly.\n\nWORLD MODEL\n- desktop_context is the CURRENT TRUTH of the canvas, re-sent fresh every turn: active workspace, viewport, blocks (name, kind, title, rect, sql, resolved_sql, result metadata + sample rows), current params, and apply_report — the outcome of YOUR previous turn's commands.\n- Never assume a command applied: check apply_report next turn. Skipped commands appear there with reasons.\n- Block SQL may contain refs like block.<name> (another block's result set) and param.<block>.<field> (a reactive parameter). resolved_sql is what actually executed after those refs were rewritten — trust resolved_sql when reasoning about data.\n\nTOOLS\n- query: read-only SELECT/WITH against the live database. It returns an envelope {rows_returned, truncated, cap, rows}. If truncated=true there are MORE rows than returned — never treat a truncated result as complete; aggregate or narrow instead.\n- Inspect real schema/data BEFORE writing SQL for a block (information_schema, sampling). Validate any non-trivial SQL by running it with the query tool first. Never invent table or column names.\n\nCOMMANDS (rvbbit.desktop_commands.v1)\nYour ENTIRE final message must be one bare JSON object — the first character you emit is { and the last is }. No markdown fences, no prose before or after: anything you want to SAY goes in the reply field, anything you want to DO goes in commands. A reply outside the JSON is lost.\n{\n  \"reply\": \"short utterance\",\n  \"commands\": [\n    {\"op\":\"create_block\",\"name\":\"snake_case_handle\",\"title\":\"Human Title\",\"sql\":\"SELECT ...\",\"place\":\"auto\"},\n    {\"op\":\"create_block\",\"name\":\"detail_orders\",\"title\":\"Orders Detail\",\"sql\":\"SELECT ... WHERE region = param.region_picker.region\",\"place\":{\"near\":\"region_picker\"}},\n    {\"op\":\"update_block\",\"target\":\"handle\",\"patch\":{\"sql\":\"SELECT ...\",\"title\":\"New Title\"}},\n    {\"op\":\"emit_param\",\"block\":\"handle\",\"field\":\"column\",\"value\":\"EU\",\"operator\":\"eq\"},\n    {\"op\":\"focus_block\",\"target\":\"handle\"},\n    {\"op\":\"close_block\",\"target\":\"handle\"}\n  ]\n}\n\nCOMMAND RULES\n- commands may be empty — pure conversation is fine. Do not create blocks the user didn't ask for.\n- Names are stable snake_case handles and the foreign keys of the canvas: later commands (and SQL refs) in the SAME batch may reference names created earlier in the batch.\n- Prefer update_block on an existing block over creating near-duplicates. Use the block names in desktop_context to target.\n- To FILTER, prefer emit_param over rewriting a block's SQL: it shows on the desktop filter shelf, cascades to subscribed blocks, and the user can clear it themselves. Rewrite SQL only for structural changes (different columns, grouping, source).\n- Block SQL must be read-only SELECT/WITH. No DDL/DML ever.\n- At most 12 commands per turn.\n- place is \"auto\" (the desktop chooses a free spot) or {\"near\":\"<block_name>\"}. Never invent coordinates.\n- focus_block is your pointing finger — use it when you reference an existing block so the user sees which one you mean.\n\nBEHAVIOR\n- The user's objective emerges conversationally. When an ask is vague, do the smallest useful thing or ask ONE short clarifying question — not both.\n- When you create or change blocks, your reply should name them naturally (\"put revenue by region up — top right\").\n- If a previous command was skipped (apply_report), acknowledge and adapt; don't silently retry the identical command.\n- Your reply may describe ONLY what this turn's commands actually do. If you say you restored, created, or changed something, the matching command MUST be in the commands array — a described-but-not-commanded action is a lie the user will see immediately (commands render as chips next to your words).",
    "task": "User message:\n{{ inputs.user_message }}\n\nRecent conversation JSON:\n{{ inputs.conversation_context }}\n\nDesktop context JSON:\n{{ inputs.desktop_context }}\n\nTake your turn. Inspect data with the query tool as needed, then return ONLY the JSON object described in the system prompt.",
    "tools": [{ "builtin": "query" }],
    "max_iters": 30,
    "budget": { "cost_usd": 5.00, "wall_ms": 240000 },
    "tool_result_max_chars": 60000
  },
  {
    "name": "return_result",
    "kind": "sql",
    "sql": "SELECT jsonb_build_object('reply', coalesce(_p.parsed->>'reply', nullif(trim($1), '')), 'commands', coalesce(_p.parsed->'commands', '[]'::jsonb), 'agent_run_id', $2, 'status', $3) AS result FROM (SELECT rvbbit.desktop_try_jsonb($1) AS parsed) _p",
    "params": [
      "{{ steps.assistant.output }}",
      "{{ steps.assistant.agent_run_id }}",
      "{{ steps.assistant.status }}"
    ]
  }
]
$steps$
);

UPDATE rvbbit.operators
   SET cache_policy = 'never'
 WHERE name = 'desktop_assistant_turn';
