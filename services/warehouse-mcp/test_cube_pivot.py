"""Focused unit contracts for direct, metric-free cube pivots."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "warehouse_server_cube_pivot_test_module",
    _HERE / "server.py",
)
server = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = server
_SPEC.loader.exec_module(server)


class _Result:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many or []


class _Connection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return self.responses.pop(0)


def _metadata_responses():
    return [
        _Result(one={"name": "sales_cube"}),
        _Result(many=[
            {"column_name": "region", "data_type": "text"},
            {"column_name": "channel", "data_type": "text"},
            {"column_name": "revenue", "data_type": "numeric"},
            {"column_name": "units", "data_type": "integer"},
        ]),
        _Result(many=[
            {
                "column_name": "region",
                "data_type": "text",
                "kind": "dimension",
                "groupable": True,
                "distinct_est": 2,
                "semantics": None,
            },
            {
                "column_name": "channel",
                "data_type": "text",
                "kind": "dimension",
                "groupable": True,
                "distinct_est": 2,
                "semantics": None,
            },
            {
                "column_name": "revenue",
                "data_type": "numeric",
                "kind": "measure",
                "groupable": False,
                "distinct_est": -1,
                "semantics": "revenue amount",
            },
            {
                "column_name": "units",
                "data_type": "integer",
                "kind": "measure",
                "groupable": False,
                "distinct_est": -1,
                "semantics": "unit count",
            },
        ]),
    ]


def test_cube_pivot_uses_numeric_field_without_metric_and_preserves_true_totals(monkeypatch):
    conn = _Connection(_metadata_responses() + [
        _Result(many=[
            {"region": "North", "channel": "Direct", "__m0": 15},
            {"region": "North", "channel": "Partner", "__m0": 10},
            {"region": "South", "channel": "Direct", "__m0": 4},
        ]),
        _Result(many=[
            {"region": "North", "__m0": 25},
            {"region": "South", "__m0": 4},
        ]),
        _Result(many=[
            {"channel": "Direct", "__m0": 19},
            {"channel": "Partner", "__m0": 10},
        ]),
        _Result(one={"__m0": 29}),
    ])
    monkeypatch.setattr(server, "_conn", lambda **_kwargs: conn)
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)

    result = server.tool_cube_pivot(
        "sales_cube",
        "region",
        "channel",
        "revenue",
        "sum",
    )

    assert "error" not in result
    assert result["display_mode"] == "crosstab"
    assert result["aggregate"] == "sum"
    assert result["available_measures"] == ["revenue", "units"]
    assert result["measures"] == [{
        "key": "sum:revenue",
        "field": "revenue",
        "aggregate": "sum",
        "label": "SUM revenue",
    }]
    assert result["columns"] == ["c0::sum:revenue", "c1::sum:revenue"]
    assert [column["label"] for column in result["value_columns"]] == [
        "Direct",
        "Partner",
    ]
    assert result["matrix"] == [
        {
            "row": "North",
            "dimensions": {"region": "North"},
            "cells": {
                "c0::sum:revenue": 15,
                "c1::sum:revenue": 10,
            },
            "totals": {"sum:revenue": 25},
            "total": 25,
        },
        {
            "row": "South",
            "dimensions": {"region": "South"},
            "cells": {"c0::sum:revenue": 4},
            "totals": {"sum:revenue": 4},
            "total": 4,
        },
    ]
    assert result["col_totals"] == {
        "c0::sum:revenue": 19,
        "c1::sum:revenue": 10,
    }
    assert result["grand_total"] == 29


def test_cube_pivot_rejects_unvalidated_axes_before_composing_data_query(monkeypatch):
    conn = _Connection(_metadata_responses())
    monkeypatch.setattr(server, "_conn", lambda **_kwargs: conn)
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)

    result = server.tool_cube_pivot(
        "sales_cube",
        'region"; DROP TABLE cubes.sales_cube; --',
        "channel",
        "revenue",
        "sum",
    )

    assert result["error"]["code"] == "BAD_AXES"
    assert len(conn.calls) == 3


def test_cube_pivot_supports_row_counts_when_cube_has_no_numeric_measure(monkeypatch):
    conn = _Connection([
        _Result(one={"name": "labels_cube"}),
        _Result(many=[
            {"column_name": "region", "data_type": "text"},
        ]),
        _Result(many=[
            {
                "column_name": "region",
                "data_type": "text",
                "kind": "dimension",
                "groupable": True,
                "distinct_est": 2,
                "semantics": None,
            },
        ]),
        _Result(many=[
            {"region": "North", "__m0": 3},
            {"region": "South", "__m0": 2},
        ]),
        _Result(one={"__m0": 5}),
    ])
    monkeypatch.setattr(server, "_conn", lambda **_kwargs: conn)
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)

    result = server.tool_cube_pivot(
        "labels_cube",
        "region",
        measure=None,
        aggregate="count",
    )

    assert result["measure"] is None
    assert result["display_mode"] == "table"
    assert result["table_columns"] == [
        {"key": "region", "label": "region", "kind": "dimension"},
        {
            "key": "count:__rows__",
            "label": "COUNT rows",
            "kind": "measure",
            "measure_key": "count:__rows__",
        },
    ]
    assert result["table_rows"][0] == {
        "dimensions": {"region": "North"},
        "values": {"count:__rows__": 3},
    }
    assert result["grand_total"] == 5


def test_cube_pivot_supports_multiple_dimensions_and_values_without_columns(monkeypatch):
    conn = _Connection(_metadata_responses() + [
        _Result(many=[
            {
                "region": "North",
                "channel": "Direct",
                "__m0": 15,
                "__m1": 2.5,
                "__m2": 2,
            },
            {
                "region": "North",
                "channel": "Partner",
                "__m0": 10,
                "__m1": 4,
                "__m2": 1,
            },
        ]),
        _Result(one={"__m0": 25, "__m1": 3, "__m2": 3}),
    ])
    monkeypatch.setattr(server, "_conn", lambda **_kwargs: conn)
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)

    result = server.tool_cube_pivot(
        "sales_cube",
        ["region", "channel"],
        measures=[
            {"field": "revenue", "aggregate": "sum"},
            {"field": "units", "aggregate": "avg"},
            {"field": None, "aggregate": "count"},
        ],
    )

    assert "error" not in result
    assert result["display_mode"] == "table"
    assert result["row_dimensions"] == ["region", "channel"]
    assert [column["label"] for column in result["table_columns"]] == [
        "region",
        "channel",
        "SUM revenue",
        "AVG units",
        "COUNT rows",
    ]
    assert result["table_rows"][0] == {
        "dimensions": {"region": "North", "channel": "Direct"},
        "values": {
            "sum:revenue": 15,
            "avg:units": 2.5,
            "count:__rows__": 2,
        },
    }
    assert result["grand_totals"] == {
        "sum:revenue": 25,
        "avg:units": 3,
        "count:__rows__": 3,
    }
