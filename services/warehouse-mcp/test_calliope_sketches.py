"""Focused contracts for Calliope's collaborative Sketch primitive."""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import uuid
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "warehouse_calliope_sketch_test_module", HERE / "calliope.py"
)
calliope = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calliope
SPEC.loader.exec_module(calliope)

SERVER_SPEC = importlib.util.spec_from_file_location(
    "warehouse_server_sketch_test_module", HERE / "server.py"
)
server = importlib.util.module_from_spec(SERVER_SPEC)
sys.modules[SERVER_SPEC.name] = server
SERVER_SPEC.loader.exec_module(server)


def sample_scene():
    return calliope._apply_sketch_operations(
        [],
        [
            {
                "op": "add_shape",
                "id": "intake",
                "shape": "rectangle",
                "x": 20,
                "y": 30,
                "width": 220,
                "height": 100,
                "label": "Intake",
            },
            {
                "op": "add_shape",
                "id": "decision",
                "shape": "diamond",
                "x": 360,
                "y": 30,
                "width": 180,
                "height": 120,
                "label": "Ready?",
            },
            {
                "op": "connect",
                "id": "review-flow",
                "from_id": "intake",
                "to_id": "decision",
                "label": "review",
            },
        ],
        1,
    )


def test_typed_operations_create_update_and_delete_a_readable_scene():
    elements, summary = sample_scene()
    assert [element["type"] for element in elements] == [
        "rectangle", "diamond", "arrow"
    ]
    assert summary == {
        "added": 3,
        "changed": 0,
        "removed": 0,
        "element_ids": ["intake", "decision", "review-flow"],
        "operation_count": 3,
    }

    changed, delta = calliope._apply_sketch_operations(
        elements,
        [
            {"op": "set_text", "id": "decision", "text": "Approved?"},
            {"op": "move", "id": "decision", "dx": 40, "dy": 15},
            {
                "op": "style",
                "id": "review-flow",
                "style": {"strokeColor": "#77c5d6", "strokeStyle": "dashed"},
            },
        ],
        2,
    )
    assert delta["changed"] == 2
    assert delta["element_ids"] == ["decision", "review-flow"]
    decision = next(item for item in changed if item["id"] == "decision")
    assert decision["x"] == 400
    assert decision["y"] == 45
    assert decision["label"] == {
        "id": "decision--label",
        "text": "Approved?",
    }

    remaining, deleted = calliope._apply_sketch_operations(
        changed, [{"op": "delete", "id": "intake"}], 3
    )
    assert {item["id"] for item in remaining} == {"decision"}
    assert deleted["removed"] == 2


def test_shape_and_connector_text_aliases_bind_and_route_without_origin_geometry():
    elements, _ = calliope._apply_sketch_operations(
        [],
        [
            {
                "op": "add_shape",
                "id": "source",
                "shape": "rectangle",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 80,
                "text": "Source",
                "style": {"stroke_color": "#2563eb", "font_size": 24},
            },
            {
                "op": "add_shape",
                "id": "target",
                "shape": "ellipse",
                "x": 500,
                "y": 300,
                "width": 200,
                "height": 80,
                "label": "Target",
                "background_color": "#dbeafe",
            },
            {
                "op": "connect",
                "id": "flow",
                "from_id": "source",
                "to_id": "target",
                "text": "flows to",
                "stroke_color": "#16a34a",
                "stroke_width": 3,
            },
        ],
        1,
    )
    source = next(item for item in elements if item["id"] == "source")
    flow = next(item for item in elements if item["id"] == "flow")
    assert source["label"] == {
        "id": "source--label",
        "text": "Source",
        "strokeColor": "#2563eb",
        "fontSize": 24,
    }
    assert flow["label"] == {
        "id": "flow--label",
        "text": "flows to",
        "strokeColor": "#16a34a",
    }
    assert flow["x"] > 200
    assert flow["y"] > 100
    assert flow["points"][0] == [0, 0]
    assert flow["points"][1] != [100, 0]

    moved, delta = calliope._apply_sketch_operations(
        elements,
        [{"op": "move", "id": "target", "dx": 100, "dy": -120}],
        2,
    )
    moved_flow = next(item for item in moved if item["id"] == "flow")
    assert moved_flow["points"] != flow["points"]
    assert delta["element_ids"] == ["target", "flow"]


def test_sketch_dsl_rejects_unknown_fields_and_cleans_deleted_bindings():
    with pytest.raises(ValueError, match=r"operation 1 \(add_text\).*font_weight"):
        calliope._apply_sketch_operations(
            [],
            [{
                "op": "add_text",
                "id": "title",
                "text": "Title",
                "font_weight": "bold",
            }],
            1,
        )

    elements, _ = sample_scene()
    for element in elements:
        if element["id"] in {"intake", "decision"}:
            element["boundElements"] = [{"id": "review-flow", "type": "arrow"}]
    remaining, _ = calliope._apply_sketch_operations(
        elements, [{"op": "delete", "id": "review-flow"}], 2
    )
    assert all(not item.get("boundElements") for item in remaining)


def test_mcp_schema_exposes_discriminated_operations_and_strict_style_fields():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("sketch-schema-test")
    mcp.tool(name="create_calliope_sketch")(server._mcp_create_calliope_sketch)
    schema = mcp._tool_manager._tools["create_calliope_sketch"].parameters
    operation_schema = schema["properties"]["operations"]["items"]
    assert operation_schema["discriminator"]["propertyName"] == "op"
    shape = schema["$defs"]["_SketchAddShape"]
    assert shape["additionalProperties"] is False
    assert "bound to and moving with the shape" in shape["properties"]["label"]["description"]
    style_fields = set(schema["$defs"]["_SketchStyleInput"]["properties"])
    assert {"stroke_color", "background_color", "font_size"} <= style_fields


@pytest.mark.parametrize(
    "elements",
    [
        [{"id": "image", "type": "image", "x": 0, "y": 0}],
        [{"id": "embed", "type": "embeddable", "x": 0, "y": 0}],
        [{"id": "bad id", "type": "text", "x": 0, "y": 0, "text": "x"}],
    ],
)
def test_scene_storage_rejects_embeds_images_and_unsafe_ids(elements):
    with pytest.raises(ValueError):
        calliope._sanitize_sketch_elements(elements)


def test_scene_storage_strips_links_and_unowned_plugin_metadata():
    sanitized = calliope._sanitize_sketch_elements([{
        "id": "safe-note",
        "type": "text",
        "x": 0,
        "y": 0,
        "text": "Useful note",
        "link": "https://example.test/private",
        "customData": {
            "otherPlugin": {"run": "something"},
            "rvbbit": {"changedBy": "human", "changedRevision": 2},
        },
    }])
    assert sanitized[0]["link"] is None
    assert sanitized[0]["customData"] == {
        "rvbbit": {"changedBy": "human", "changedRevision": 2}
    }


def test_sketch_tool_results_project_as_one_revision_lineage():
    sketch_id = str(uuid.uuid4())
    projected = calliope._project_tool_result(
        "update_calliope_sketch",
        {
            "updated": True,
            "sketch": {
                "id": sketch_id,
                "title": "Admissions handoff",
                "revision": 4,
                "element_count": 8,
                "last_actor": "calliope",
                "last_operation_count": 2,
                "last_change_summary": {"changed": 2, "element_ids": ["a", "b"]},
                "can_undo_calliope": True,
            },
            "surface": {
                "mode": "collaborative_sketch",
                "sketch_id": sketch_id,
                "revision": 4,
                "element_count": 8,
            },
        },
        {"session_id": str(uuid.uuid4()), "sketch_id": sketch_id, "expected_revision": 3},
        "sketch-call-4",
    )
    assert len(projected) == 1
    assert projected[0]["kind"] == "sketch"
    assert projected[0]["lineage_key"] == f"sketch:{sketch_id}"
    assert projected[0]["payload"]["revision"] == 4
    assert projected[0]["presentation"] == {"view": "collaborative_sketch"}


def test_new_web_notebook_bootstraps_one_hidden_collapsed_sketch():
    session_id = str(uuid.uuid4())
    statements = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, statement, params):
            statements.append((statement, params))
            if "SELECT id FROM rvbbit.calliope_sessions" in statement:
                return Result({"id": session_id})
            if "SELECT * FROM rvbbit.calliope_sketches" in statement:
                return Result(None)
            if "INSERT INTO rvbbit.calliope_sketches" in statement:
                return Result({
                    "id": params[0],
                    "session_id": session_id,
                    "owner_email": "person@example.com",
                    "title": "Shared Sketch",
                    "revision": 1,
                    "elements": [],
                    "app_state": {},
                    "last_actor": "calliope",
                    "last_actor_email": "person@example.com",
                    "last_operation_count": 0,
                    "last_change_summary": {"bootstrap": True},
                })
            return Result()

    sketch = calliope._insert_default_session_sketch(
        Connection(), "person@example.com", session_id
    )

    assert sketch["title"] == "Shared Sketch"
    assert sketch["element_count"] == 0
    turn_sql = next(sql for sql, _params in statements if "INSERT INTO rvbbit.calliope_turns" in sql)
    assert "sketch_bootstrap" in turn_sql
    assert ",0," in turn_sql
    surface_params = next(
        params for sql, params in statements
        if "INSERT INTO rvbbit.calliope_surfaces" in sql
    )
    payload = json.loads(surface_params[6])
    assert payload["auto_created"] is True
    assert payload["default_collapsed"] is True
    assert payload["element_count"] == 0

    creator_source = inspect.getsource(calliope._create_session_record)
    backend_source = (HERE / "calliope.py").read_text(encoding="utf-8")
    parent_source = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    assert "default_sketch: bool = False" in creator_source
    assert "_insert_default_session_sketch(conn, owner, local_id)" in creator_source
    assert "default_sketch=True" in backend_source
    assert "initializeDefaultSketchCollapse(surface)" in parent_source
    assert "sketchCollapseInitialized" in parent_source


def test_create_tool_adopts_the_untouched_default_sketch():
    session_id = str(uuid.uuid4())
    sketch_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    statements = []
    existing = {
        "id": sketch_id,
        "session_id": session_id,
        "owner_email": "person@example.com",
        "title": "Shared Sketch",
        "revision": 1,
        "elements": [],
        "app_state": {},
        "last_actor": "calliope",
        "last_actor_email": "person@example.com",
        "last_operation_count": 0,
        "last_change_summary": {"bootstrap": True},
    }

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, statement, params):
            statements.append((statement, params))
            if "SELECT id,owner_email FROM rvbbit.calliope_sessions" in statement:
                return Result({"id": session_id, "owner_email": "person@example.com"})
            if "SELECT * FROM rvbbit.calliope_sketches" in statement:
                return Result(existing)
            if "status='running'" in statement:
                return Result({"id": turn_id})
            if "UPDATE rvbbit.calliope_sketches SET" in statement:
                return Result({
                    **existing,
                    "title": params[0],
                    "revision": params[1],
                    "elements": json.loads(params[2]),
                    "last_operation_count": params[4],
                    "last_change_summary": json.loads(params[5]),
                })
            return Result()

    result = calliope.create_sketch(
        lambda: Connection(),
        "person@example.com",
        session_id,
        "Intake map",
        [{"op": "add_text", "id": "note", "text": "Start here", "x": 10, "y": 20}],
    )

    assert result["created"] is False
    assert result["reused"] is True
    assert result["sketch"]["id"] == sketch_id
    assert result["sketch"]["revision"] == 2
    assert result["sketch"]["element_count"] == 1
    assert not any(
        "INSERT INTO rvbbit.calliope_sketches" in sql for sql, _params in statements
    )
    assert any(
        "UPDATE rvbbit.calliope_surfaces" in sql for sql, _params in statements
    )


def test_unselected_sketch_receipt_remains_resolvable_in_later_turns():
    session_id = str(uuid.uuid4())
    sketch_id = str(uuid.uuid4())
    surface_id = str(uuid.uuid4())

    class Result:
        @staticmethod
        def fetchall():
            return [{
                "id": surface_id,
                "kind": "sketch",
                "title": "Shared Sketch",
                "artifact_slug": None,
                "artifact_version": None,
                "lineage_key": f"sketch:{sketch_id}",
                "payload": {
                    "sketch_id": sketch_id,
                    "revision": 4,
                    "element_count": 7,
                    "last_actor": "human",
                },
                "source": {"origin": "calliope_session_default"},
                "created_at": "2026-08-10T12:00:00Z",
            }]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(_statement, _params):
            return Result()

    compact, selected = calliope._compact_surface_context(
        lambda: Connection(), session_id, None
    )

    assert selected is None
    assert compact[0]["sketch"]["id"] == sketch_id
    assert compact[0]["sketch"]["revision"] == 4
    assert compact[0]["sketch"]["reader"] == {
        "tool": "read_calliope_sketch",
        "arguments": {"session_id": session_id, "sketch_id": sketch_id},
    }


def test_sketch_runtime_maps_adaptive_palette_without_replacing_typography():
    source_root = HERE / "calliope-sketch" / "src"
    theme = (source_root / "theme.ts").read_text(encoding="utf-8")
    main = (source_root / "main.tsx").read_text(encoding="utf-8")
    stylesheet = (source_root / "sketch.css").read_text(encoding="utf-8")
    parent_stylesheet = (HERE / "calliope" / "calliope.css").read_text(
        encoding="utf-8"
    )

    assert "setTheme(applyAdaptiveSketchTheme(event.data))" in main
    assert 'root.dataset.adaptiveTheme = applied ? "true" : "fallback"' in theme
    assert '"--panel"' in theme
    assert '"--amber"' in theme
    assert '"--danger"' in theme
    assert "text.length > 240" in theme
    assert "url\\s*\\(" in theme

    assert "--color-primary: var(--sketch-accent)" in stylesheet
    assert "--island-bg-color:" in stylesheet
    assert "--popup-bg-color:" in stylesheet
    assert "--color-danger: var(--sketch-danger)" in stylesheet
    assert "backdrop-filter: blur(18px)" in stylesheet
    assert 'document.documentElement.dataset.embedded = String(window.parent !== window)' in main
    assert 'return "transparent"' in main
    assert "viewBackgroundColor: exportBackgroundFor(theme)" in main
    assert ':root[data-embedded="true"]' in stylesheet
    assert "background: transparent" in stylesheet
    assert "backdrop-filter:blur(24px)" in parent_stylesheet
    assert "--ui-font" not in stylesheet
    assert "--theme-filter" not in stylesheet


def test_sketch_runtime_disables_the_excalidraw_library_surface():
    source_root = HERE / "calliope-sketch" / "src"
    main = (source_root / "main.tsx").read_text(encoding="utf-8")
    stylesheet = (source_root / "sketch.css").read_text(encoding="utf-8")

    assert 'sidebar?.name === "default"' in main
    assert 'sidebar.tab === "library"' in main
    assert "updateScene({ appState: { openSidebar: null } })" in main
    assert ".default-sidebar-trigger" in stylesheet
    assert 'li[data-testid="addToLibrary"]' in stylesheet


def test_sketch_runtime_opens_in_zen_mode_by_default():
    main = (HERE / "calliope-sketch" / "src" / "main.tsx").read_text(
        encoding="utf-8"
    )

    assert "zenModeEnabled: true" in main
    assert 'for (const key of ["gridSize", "gridStep", "gridModeEnabled"])' in main


def test_sketch_runtime_can_move_the_live_editor_into_a_large_workspace():
    inner = (HERE / "calliope-sketch" / "src" / "main.tsx").read_text(
        encoding="utf-8"
    )
    parent = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    markup = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    stylesheet = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert 'id="sketch-workspace-dialog"' in markup
    assert 'id="sketch-workspace-content"' in markup
    assert "data-expand-sketch" in parent
    assert "els.sketchWorkspace.append(card)" in parent
    assert "els.sketchShelf.append(card)" in parent
    assert "data-close-sketch-workspace" in parent
    assert "calliope.sketch.expand.request" in inner
    assert "calliope.sketch.viewport.changed" in inner
    assert ".sketch-workspace-dialog::backdrop" in stylesheet
    assert ".sketch-workspace-content .sketch-stage-frame" in stylesheet
    assert ".sketch-workspace-close:hover" in stylesheet


def test_sketch_is_a_stage_dock_and_collapse_is_a_minimized_rail():
    parent = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    markup = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    stylesheet = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert markup.index('id="sketch-shelf"') < markup.index('id="stage-scroll"')
    assert "sketchMinimized ? \"sketch-minimized\"" in parent
    assert 'aria-expanded="${!sketchMinimized}"' in parent
    assert '[data-sketch-card].sketch-minimized' in parent
    assert ".sketch-shelf .surface.kind-sketch.sketch-minimized" in stylesheet
    assert ".sketch-shelf .surface.kind-sketch>.surface-lineage{display:none}" in stylesheet
    assert "position:sticky" not in stylesheet.split(".sketch-shelf{", 1)[1].split("}", 1)[0]


def test_proactive_sketch_permission_is_scoped_to_the_calliope_web_notebook():
    instructions = calliope._instructions([], None)
    source = (HERE / "calliope.py").read_text(encoding="utf-8")
    tool_help = inspect.getdoc(server._mcp_create_calliope_sketch) or ""

    assert "PROACTIVE SKETCHES — CALLIOPE UI ONLY" in instructions
    assert "without waiting for the user to ask" in instructions
    assert "Do not ask permission first" in instructions
    assert "do not add a Sketch merely to decorate" in instructions
    assert "This proactive permission does not apply outside" in instructions
    assert "This request originates in the Calliope web notebook" in source

    assert "proactive Sketch creation is authorized only" in server._INSTRUCTIONS
    assert "another direct MCP client" in server._INSTRUCTIONS
    assert "only when the human explicitly requests one" in server._INSTRUCTIONS
    assert "in other clients, use this only after an explicit human request" in tool_help


def test_sketch_tools_and_lazy_stage_island_ship_as_one_contract():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    sketch_page = (HERE / "calliope" / "sketch.html").read_text(encoding="utf-8")
    sketch_bootstrap = (HERE / "calliope" / "sketch-bootstrap.js").read_text(
        encoding="utf-8"
    )
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    runtime = HERE / "calliope" / "sketch-runtime.js"
    stylesheet = HERE / "calliope" / "sketch-runtime.css"

    for function in (
        server._mcp_create_calliope_sketch,
        server._mcp_read_calliope_sketch,
        server._mcp_update_calliope_sketch,
    ):
        assert "owner" not in inspect.signature(function).parameters
        assert "email" not in inspect.signature(function).parameters
    assert 'mcp.tool(name="create_calliope_sketch")' in server_source
    assert 'mcp.tool(name="read_calliope_sketch")' in server_source
    assert 'mcp.tool(name="update_calliope_sketch")' in server_source
    assert '"/api/calliope/sketches/{sketch_id}"' in backend
    assert '"/api/calliope/sketches/{sketch_id}/undo-calliope"' in backend
    assert '"/api/calliope/sketches/{sketch_id}/preview"' in backend
    assert 'id="sketch-shelf"' in page
    assert 'data-sketch-id="__SKETCH_ID__"' in sketch_page
    assert "/calliope/sketch-assets/" in sketch_bootstrap
    assert (HERE / "calliope" / "sketch-assets" / "fonts" / "Excalifont").is_dir()
    assert "function renderSketchShelf" in script
    assert "calliope.sketch.saved" in script
    assert "broadcastViewerThemeToSketches" in script
    assert runtime.stat().st_size > 1_000_000
    assert stylesheet.stat().st_size > 100_000
