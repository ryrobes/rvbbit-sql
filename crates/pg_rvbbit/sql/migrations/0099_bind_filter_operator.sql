-- 0099_bind_filter_operator
--
-- Seed a SQL-native binding operator. It preserves an existing UI artifact
-- rowset and appends a hidden filter_binding meta artifact.

SELECT rvbbit.create_operator(
    op_name        => 'bind_filter',
    op_arg_names   => ARRAY['target','field','operator','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_bind_filter',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'target', '{{ inputs.target }}',
            'field', '{{ inputs.field }}',
            'operator', '{{ inputs.operator }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline meta stage: bind a control artifact to a named target tile for cross-filtering.'
);

SELECT rvbbit.flush_cache();
