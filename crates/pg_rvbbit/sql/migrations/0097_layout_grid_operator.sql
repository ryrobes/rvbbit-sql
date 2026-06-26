-- 0097_layout_grid_operator
--
-- Seed a SQL-native layout meta operator. It emits a UI artifact that the
-- Lens multi-statement renderer can interpret as an arranged tile layout.

SELECT rvbbit.create_operator(
    op_name        => 'layout_grid',
    op_arg_names   => ARRAY['layout','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_layout_grid',
        'inputs', jsonb_build_object(
            'layout', '{{ inputs.layout }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline meta stage: emit a statement-grid layout artifact for multi-statement UI composition.'
);

SELECT rvbbit.flush_cache();
