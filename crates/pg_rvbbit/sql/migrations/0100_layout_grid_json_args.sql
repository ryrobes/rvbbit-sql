-- 0100_layout_grid_json_args
--
-- Extend layout_grid without changing its old positional contract:
-- positional args remain (layout, title), while named rows/mode allow
-- structured SQL-authored mini-app layout metadata.

SELECT rvbbit.create_operator(
    op_name        => 'layout_grid',
    op_arg_names   => ARRAY['layout','title','rows','mode'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_layout_grid',
        'inputs', jsonb_build_object(
            'layout', '{{ inputs.layout }}',
            'title', '{{ inputs.title }}',
            'layout_rows', '{{ inputs.rows }}',
            'mode', '{{ inputs.mode }}'
        )
    )),
    op_description => 'Pipeline meta stage: emit a statement-grid layout artifact for multi-statement UI composition.'
);

SELECT rvbbit.flush_cache();
