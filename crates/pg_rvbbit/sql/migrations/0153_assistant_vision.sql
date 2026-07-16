-- 0153_assistant_vision.sql
-- Give the Desktop Assistant a vision-capable overload whose fourth operator
-- input carries private, base64-encoded screenshots for the current turn. The
-- old three-argument wrapper remains usable by older Lens clients and text-only
-- turns. The agent transport validates and caps the image parts before placing
-- them on the OpenAI-compatible multimodal message.

DO $migration$
DECLARE
    op rvbbit.operators%ROWTYPE;
    next_arg_names text[];
    next_arg_types text[];
    next_steps jsonb;
    result_sql text;
BEGIN
    SELECT * INTO op
      FROM rvbbit.operators
     WHERE name = 'desktop_assistant_turn';

    IF NOT FOUND THEN
        RETURN;
    END IF;

    next_arg_names := CASE
        WHEN 'vision_attachments' = ANY(op.arg_names) THEN op.arg_names
        ELSE array_append(op.arg_names, 'vision_attachments')
    END;
    next_arg_types := CASE
        WHEN cardinality(op.arg_types) >= cardinality(next_arg_names) THEN op.arg_types
        ELSE array_append(op.arg_types, 'jsonb')
    END;

    next_steps := jsonb_set(
        op.steps,
        '{0,vision}',
        to_jsonb('{{ inputs.vision_attachments }}'::text),
        true
    );
    IF coalesce(next_steps->0->>'system', '') NOT LIKE '%VISUAL FEEDBACK%' THEN
        next_steps := jsonb_set(
            next_steps,
            '{0,system}',
            to_jsonb(
                coalesce(next_steps->0->>'system', '')
                || E'\n\nVISUAL FEEDBACK\n'
                || E'- A user turn may include screenshots of a block''s currently rendered viewport. Treat them as direct visual evidence of what the user sees, including layout, clipping, empty states, legibility, and styling.\n'
                || E'- The screenshot metadata in recent conversation is historical context; only images attached to the current turn are actually visible to you.\n'
                || E'- When repairing an app from a screenshot, update the existing block and preserve its stable query ids unless the data contract genuinely changes.'
            ),
            true
        );
    END IF;

    result_sql := next_steps->1->>'sql';
    IF result_sql NOT LIKE '%assistant_attachments%' THEN
        result_sql := format(
            'SELECT /* assistant_attachments */ (_assistant_result.result || jsonb_build_object(''attachments'', coalesce($5::jsonb, ''[]''::jsonb))) AS result FROM (%s) _assistant_result',
            result_sql
        );
        next_steps := jsonb_set(next_steps, '{1,sql}', to_jsonb(result_sql), true);
        next_steps := jsonb_set(
            next_steps,
            '{1,params}',
            coalesce(next_steps->1->'params', '[]'::jsonb)
              || jsonb_build_array('{{ steps.assistant.attachments }}'),
            true
        );
    END IF;

    PERFORM rvbbit.create_operator(
        op_name          => op.name,
        op_arg_names     => next_arg_names,
        op_return_type   => op.return_type,
        op_system        => op.system_prompt,
        op_user          => op.user_prompt,
        op_shape         => op.shape,
        op_model         => op.model,
        op_parser        => op.parser,
        op_max_tokens    => op.max_tokens,
        op_temperature   => op.temperature,
        op_arg_types     => next_arg_types,
        op_description   => op.description,
        op_infix_symbol  => op.infix_symbol,
        op_infix_word    => op.infix_word,
        op_tests         => op.tests,
        op_steps         => next_steps
    );
END
$migration$;
