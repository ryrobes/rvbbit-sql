-- 0098_tile_name_operator
--
-- Seed a SQL-native component alias operator. It preserves an existing UI
-- artifact rowset and appends a hidden statement_name meta artifact.

SELECT rvbbit.create_operator(
    op_name        => 'tile_name',
    op_arg_names   => ARRAY['name','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_tile_name',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'name', '{{ inputs.name }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline meta stage: attach a stable layout alias to the current UI artifact statement.'
);

SELECT rvbbit.flush_cache();
