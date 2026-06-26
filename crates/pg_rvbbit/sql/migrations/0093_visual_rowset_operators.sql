-- 0093_visual_rowset_operators
--
-- Existing installs do not replay pipeline.sql when the extension binary is
-- rebuilt. Seed the visual rowset operators through the normal idempotent
-- migration path so deploys can run SELECT rvbbit.migrate() without dropping
-- the extension or cascading user data.

SELECT rvbbit.create_operator(
    op_name        => 'metric_card',
    op_arg_names   => ARRAY['label','value','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_metric_card',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'label', '{{ inputs.label }}',
            'value', '{{ inputs.value }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a metric-card UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'bar_chart',
    op_arg_names   => ARRAY['x','y','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_bar_chart',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'x', '{{ inputs.x }}',
            'y', '{{ inputs.y }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a Vega-Lite bar-chart UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'table_view',
    op_arg_names   => ARRAY['title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_table_view',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a table UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'vega_lite',
    op_arg_names   => ARRAY['spec','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_vega_lite',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'spec', '{{ inputs.spec }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a custom Vega-Lite UI artifact from the current resultset.'
);

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
