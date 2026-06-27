-- 0102_filter_control_defaults
--
-- Extend filter_control with optional default/value hints. These compile into
-- the same UI artifact row contract; the Lens renderer decides whether to seed
-- an initial pick parameter from spec.default_value.

SELECT rvbbit.create_operator(
    op_name        => 'filter_control',
    op_arg_names   => ARRAY['field','kind','title','operator','default','value','default_value'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_filter_control',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'field', '{{ inputs.field }}',
            'kind', '{{ inputs.kind }}',
            'title', '{{ inputs.title }}',
            'operator', '{{ inputs.operator }}',
            'default', '{{ inputs.default }}',
            'value', '{{ inputs.value }}',
            'default_value', '{{ inputs.default_value }}'
        )
    )),
    op_description => 'Pipeline control stage: emit a parameter-publishing filter-control UI artifact from the current resultset.'
);

SELECT rvbbit.flush_cache();
