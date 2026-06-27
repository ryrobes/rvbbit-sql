-- 0103_action_button_operator
--
-- Add the first SQL-authored action control. The operator emits a UI artifact;
-- clients decide how to execute the spec safely.

SELECT rvbbit.create_operator(
    op_name        => 'action_button',
    op_arg_names   => ARRAY['label','sql','title','confirm','variant','refresh'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_action_button',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'label', '{{ inputs.label }}',
            'sql', '{{ inputs.sql }}',
            'title', '{{ inputs.title }}',
            'confirm', '{{ inputs.confirm }}',
            'variant', '{{ inputs.variant }}',
            'refresh', '{{ inputs.refresh }}'
        )
    )),
    op_description => 'Pipeline action stage: emit a SQL action button UI artifact from the current resultset.'
);

SELECT rvbbit.flush_cache();
