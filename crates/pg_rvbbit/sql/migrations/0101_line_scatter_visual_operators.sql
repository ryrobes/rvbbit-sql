-- 0101_line_scatter_visual_operators
--
-- Add typed visual shortcuts that still emit the same vega_lite UI artifact
-- row contract as bar_chart.

SELECT rvbbit.create_operator(
    op_name        => 'line_chart',
    op_arg_names   => ARRAY['x','y','title','color'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_line_chart',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'x', '{{ inputs.x }}',
            'y', '{{ inputs.y }}',
            'title', '{{ inputs.title }}',
            'color', '{{ inputs.color }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a Vega-Lite line-chart UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'scatter_plot',
    op_arg_names   => ARRAY['x','y','title','color','size'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_scatter_plot',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'x', '{{ inputs.x }}',
            'y', '{{ inputs.y }}',
            'title', '{{ inputs.title }}',
            'color', '{{ inputs.color }}',
            'size', '{{ inputs.size }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a Vega-Lite scatter-plot UI artifact from the current resultset.'
);

SELECT rvbbit.flush_cache();
