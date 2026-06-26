-- 0096_filter_control_operator
--
-- Some installs applied 0093_visual_rowset_operators before filter_control was
-- added to that migration. Because applied migrations are recorded and never
-- replayed, seed the control operator in its own forward-only migration.

SELECT rvbbit.create_operator(
    op_name        => 'filter_control',
    op_arg_names   => ARRAY['field','kind','title','operator'],
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
            'operator', '{{ inputs.operator }}'
        )
    )),
    op_description => 'Pipeline control stage: emit a parameter-publishing filter-control UI artifact from the current resultset.'
);

SELECT rvbbit.flush_cache();
