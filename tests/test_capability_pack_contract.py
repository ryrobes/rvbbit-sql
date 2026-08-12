"""Catalog pack metadata contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

yaml = pytest.importorskip("yaml")


PACKS = Path(__file__).resolve().parents[1] / "capabilities" / "packs"
ROOT = PACKS.parent.parent


def _pack_docs():
    for path in sorted(PACKS.rglob("rvbbit-pack.yaml")):
        with path.open(encoding="utf-8") as fh:
            yield path, yaml.safe_load(fh)


def _capability_doc(path: Path):
    with (path.parent / "capability.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_pack_exports_include_manifest_operators():
    bad: list[str] = []
    for path, pack in _pack_docs():
        capability_path = path.parent / "capability.yaml"
        if not capability_path.exists():
            continue
        manifest = _capability_doc(path)
        manifest_ops = {op["name"] for op in manifest.get("operators") or []}
        if not manifest_ops:
            continue
        exported_ops = set((pack.get("exports") or {}).get("operators") or [])
        missing = sorted(manifest_ops - exported_ops)
        extra = sorted(exported_ops - manifest_ops)
        if missing or extra:
            bad.append(
                f"{path.relative_to(PACKS.parent.parent)} "
                f"missing={missing} extra={extra}"
            )

    assert not bad, "pack exports.operators must mirror capability operators: " + "; ".join(bad)


def test_generated_compose_does_not_publish_default_host_port():
    pack = PACKS / "rerank" / "bge-reranker-base"
    proc = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "compose",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "expose:" in proc.stdout
    assert "ports:" not in proc.stdout


def test_sql_test_pack_renders_without_backend_or_sidecar():
    pack = PACKS / "sql" / "core-workflows"
    register = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "register",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    compose = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "compose",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout

    assert "register_backend" not in register
    assert "register_python_runtime" not in register
    assert "No backend or runtime is registered" in register
    assert "services:" not in compose
    assert "No Docker sidecar" in compose


def test_generated_operator_sql_replaces_infix_bindings():
    pack = PACKS / "rerank" / "bge-reranker-base"
    proc = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "operators",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "op.oprname = '~~%'" in proc.stdout
    assert "op.oprname = '~~?'" in proc.stdout
    assert "SET infix_symbol = '~~%'" in proc.stdout
    assert "SET infix_symbol = '~~?'" in proc.stdout


def test_llm_provider_pack_renders_chat_backend_and_provider_registration():
    pack = PACKS / "llm" / "gemma-4-12b-it-vllm"
    register = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "register",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert "backend_transport        => 'openai_chat'" in register
    assert "rvbbit.register_self_hosted_model" in register
    assert "provider           => 'gemma_4_12b_it'" in register
    assert "model              => 'google/gemma-4-12B-it'" in register
    assert "rvbbit.set_default_provider" not in register

    compose = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "compose",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert 'image: "vllm/vllm-openai:latest"' in compose
    assert "command:" in compose
    assert '      - "--model"' in compose
    assert '      - "google/gemma-4-12B-it"' in compose
    assert 'ipc: "host"' in compose
    assert "ports:" not in compose


def test_managed_clover_uses_a_stable_service_model_alias():
    capability = json.loads(
        (PACKS / "managed" / "clover" / "capability.json").read_text(encoding="utf-8")
    )
    install_sql = "\n".join(capability["managed"]["install"]["sql"])

    assert '"model": "clover"' in install_sql
    assert '"model_policy": "managed"' in install_sql
    assert '"model_aliases": ["gemma4"]' in install_sql
    assert "'provider','clover_llm','model','gemma4'" not in install_sql
    assert '"provider":"clover_llm","model":"gemma4"' not in install_sql
    assert "nvidia/Gemma-4-31B-IT-NVFP4" not in install_sql
    assert "register_backend('embed',     'https://clover.rvbb.it/v1/embeddings'" in install_sql
    assert "'openai', 32, 25, 30000, 'RVBBIT_CLOVER_KEY'" in install_sql
    assert "http://clover.rvbb.it:8090" not in install_sql


def test_clover_edge_is_pinned_and_terminates_tls_in_front_of_hutch():
    compose = yaml.safe_load(
        (ROOT / "docker" / "docker-compose.clover-edge.yml").read_text(
            encoding="utf-8"
        )
    )
    caddy = compose["services"]["caddy"]
    assert caddy["image"].startswith("caddy@sha256:")
    assert caddy["network_mode"] == "host"
    assert "./clover/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]

    caddyfile = (ROOT / "docker" / "clover" / "Caddyfile").read_text(
        encoding="utf-8"
    )
    assert "clover.rvbb.it" in caddyfile
    assert "reverse_proxy 127.0.0.1:8090" in caddyfile


def test_google_meet_pack_renders_as_a_governed_brain_connector():
    pack = PACKS / "integrations" / "google-meet-brain"
    register = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "register",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    compose = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "compose",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout

    assert "rvbbit.register_backend" in register
    assert "rvbbit.brain_configure_source" in register
    assert "'gmeet_connector'" in register
    assert '"acl_mode":"calendar_invitees_strict"' in register
    assert '"summarize_meetings":true' in register
    assert "rvbbit.register_python_runtime" not in register
    assert "rvbbit.register_memory_service" not in register
    assert "rvbbit-gmeet-connector:latest" in compose
    assert 'GMEET_ACL_MODE: "${GMEET_ACL_MODE:-calendar_invitees_strict}"' in compose

    seed = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "catalog",
            "seed-json",
            "--root",
            str(PACKS),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    item = next(
        row for row in json.loads(seed.stdout)["capabilities"]
        if row["catalog_entry"]["id"] == "integrations/google-meet-brain"
    )
    assert item["catalog_entry"]["install_mode"] == "image"
    assert item["catalog_entry"]["backend_name"] == "gmeet_connector"
    assert item["capability_manifest"]["runtime"]["language"] == "brain_connector"
    assert item["capability_manifest"]["brain_source"]["config"]["acl_mode"] == (
        "calendar_invitees_strict"
    )


def test_deploy_manifest_preserves_smoke_probe_inputs():
    pack = PACKS / "tabular" / "wine-quality-sklearn"
    proc = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "deploy",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert '"smoke"' in proc.stdout
    assert "fixed acidity" in proc.stdout


def test_scaffold_includes_handler_extra_requirements(tmp_path):
    pack = PACKS / "extract" / "gliner-medium-v2.1"
    out = tmp_path / "gliner"
    subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "scaffold",
            str(pack),
            str(out),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    requirements = (out / "requirements.txt").read_text(encoding="utf-8")
    assert "gliner==0.2.16" in requirements


def test_builtin_packs_have_acceptance_sql():
    missing: list[str] = []
    invalid: list[str] = []
    for path, pack in _pack_docs():
        acceptance = pack.get("acceptance") or {}
        tests = acceptance.get("tests") or []
        if not tests:
            missing.append(str(path.relative_to(PACKS.parent.parent)))
            continue
        for test in tests:
            name = test.get("name") if isinstance(test, dict) else None
            sql = test.get("sql") if isinstance(test, dict) else None
            if not name or not isinstance(sql, str) or not sql.strip():
                invalid.append(f"{path.relative_to(PACKS.parent.parent)}:{name}")

    assert not missing, "built-in packs without acceptance.tests: " + ", ".join(missing)
    assert not invalid, "invalid acceptance tests: " + ", ".join(invalid)


def test_acceptance_sql_covers_exported_operators():
    gaps: list[str] = []
    for path, pack in _pack_docs():
        operators = (pack.get("exports") or {}).get("operators") or []
        if not operators:
            continue
        acceptance = pack.get("acceptance") or {}
        haystack = "\n".join(
            test.get("sql", "")
            for test in acceptance.get("tests") or []
            if isinstance(test, dict)
        )
        missing = []
        for op in operators:
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_])(?:rvbbit\.)?{re.escape(op)}\s*\(",
                re.IGNORECASE,
            )
            if not pattern.search(haystack):
                missing.append(op)
        if missing:
            gaps.append(f"{path.relative_to(PACKS.parent.parent)} missing={missing}")

    assert not gaps, "acceptance SQL should call every exported operator: " + "; ".join(gaps)


def test_acceptance_targets_are_explicit_selectors():
    bad: list[str] = []
    for path, pack in _pack_docs():
        acceptance = pack.get("acceptance") or {}
        target = acceptance.get("target_selector") or {}
        if not isinstance(target, dict):
            bad.append(str(path.relative_to(PACKS.parent.parent)))
            continue
        if not acceptance.get("tests"):
            continue
        if target.get("capability") is True:
            continue
        if target.get("sql") is True and target.get("capability") is False:
            continue
        bad.append(str(path.relative_to(PACKS.parent.parent)))

    assert not bad, "acceptance target_selector should be capability:true or sql:true/capability:false: " + ", ".join(bad)


def test_operator_runtime_packs_are_flagged():
    packs = {pack["id"]: (path, pack) for path, pack in _pack_docs()}
    expected = {"runtimes/python-runtime", "runtimes/mcp-gateway"}
    missing = expected - packs.keys()
    assert not missing, "missing operator runtime packs: " + ", ".join(sorted(missing))

    bad: list[str] = []
    for pack_id in expected:
        path, pack = packs[pack_id]
        tags = set(pack.get("tags") or [])
        if (
            pack.get("system_runtime") is not True
            or pack.get("capability_role") != "operator_runtime"
            or "system-runtime" not in tags
            or "operator-runtime" not in tags
        ):
            bad.append(str(path.relative_to(PACKS.parent.parent)))

    assert not bad, "operator runtime packs missing system-runtime metadata: " + ", ".join(bad)


def test_huggingface_model_packs_publish_gpu_weight_estimates():
    missing: list[str] = []
    invalid: list[str] = []
    for path, pack in _pack_docs():
        source = pack.get("source") or {}
        runtime = pack.get("runtime") or {}
        if source.get("provider") != "huggingface" or runtime.get("device") == "cpu":
            continue
        gpu = ((pack.get("resources") or {}).get("gpu") or {})
        if not gpu:
            missing.append(str(path.relative_to(PACKS.parent.parent)))
            continue
        if (
            gpu.get("placement") != "single_gpu"
            or int(gpu.get("model_size_bytes") or 0) <= 0
            or int(gpu.get("vram_required_bytes") or 0) <= int(gpu.get("model_size_bytes") or 0)
        ):
            invalid.append(str(path.relative_to(PACKS.parent.parent)))

    assert not missing, "HF model packs missing resources.gpu estimates: " + ", ".join(missing)
    assert not invalid, "invalid resources.gpu estimates: " + ", ".join(invalid)


def test_catalog_json_flattens_gpu_weight_estimates():
    proc = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "catalog",
            "build",
            "--root",
            str(PACKS),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    doc = json.loads(proc.stdout)
    by_id = {entry["id"]: entry for entry in doc["capabilities"]}

    bad: list[str] = []
    for path, pack in _pack_docs():
        source = pack.get("source") or {}
        runtime = pack.get("runtime") or {}
        if source.get("provider") != "huggingface" or runtime.get("device") == "cpu":
            continue
        gpu = ((pack.get("resources") or {}).get("gpu") or {})
        entry = by_id.get(pack["id"])
        expected_required = bool(gpu.get("required")) or runtime.get("device") == "cuda"
        if not entry:
            bad.append(f"{path.relative_to(ROOT)} missing catalog entry")
            continue
        if (
            entry.get("gpu_required") != expected_required
            or entry.get("gpu_placement") != gpu.get("placement")
            or entry.get("model_size_bytes") != gpu.get("model_size_bytes")
            or entry.get("vram_required_bytes") != gpu.get("vram_required_bytes")
            or entry.get("vram_headroom_pct") != gpu.get("headroom_pct")
        ):
            bad.append(str(path.relative_to(ROOT)))

    assert not bad, "catalog JSON must expose flattened GPU weight estimates: " + "; ".join(bad)


def test_managed_clover_pack_ships_refreshable_offline_snapshot():
    proc = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "catalog",
            "seed-json",
            "--root",
            str(PACKS),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    doc = json.loads(proc.stdout)
    item = next(
        entry
        for entry in doc["capabilities"]
        if entry["catalog_entry"]["id"] == "managed/clover"
    )
    catalog = item["catalog_entry"]
    manifest = item["capability_manifest"]

    assert catalog["catalog_url"] == "https://rvbbit.ai/catalog.json"
    assert catalog["catalog_refresh_seconds"] == 21600
    assert catalog["catalog_seed_snapshot"] is True
    assert "clover_embed" in catalog["operators"]
    assert any(
        "clover_embed" in statement
        for statement in manifest["managed"]["install"]["sql"]
    )

    pack = PACKS / "managed" / "clover"
    register = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "register",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    compose = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "render",
            "--part",
            "compose",
            str(pack),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert "create_operator('clover_embed'" in register
    assert "services:" not in compose
    assert "no Docker sidecar" in compose


def test_release_catalog_can_emit_image_mode_runtimes():
    proc = subprocess.run(
        [
            str(ROOT / "capabilities" / "tools" / "rvbbit-capability"),
            "catalog",
            "seed-json",
            "--root",
            str(PACKS),
            "--image-prefix",
            "ghcr.io/ryrobes",
            "--image-tag",
            "9.9.9",
            "--default-install-mode",
            "image",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    doc = json.loads(proc.stdout)
    by_id = {entry["catalog_entry"]["id"]: entry for entry in doc["capabilities"]}

    python_runtime = by_id["runtimes/python-runtime"]
    assert python_runtime["catalog_entry"]["runtime_mode"] == "image"
    assert python_runtime["catalog_entry"]["runtime_image"] == (
        "ghcr.io/ryrobes/rvbbit-python-runtime:9.9.9"
    )
    assert python_runtime["capability_manifest"]["runtime"]["mode"] == "image"
    assert python_runtime["capability_manifest"]["runtime"]["image"] == (
        "ghcr.io/ryrobes/rvbbit-python-runtime:9.9.9"
    )

    reranker = by_id["rerank/bge-reranker-base"]
    assert reranker["catalog_entry"]["runtime_image"] == (
        "ghcr.io/ryrobes/rvbbit-capability-bge-reranker-base:9.9.9"
    )
    assert reranker["capability_manifest"]["runtime"]["image"] == (
        "ghcr.io/ryrobes/rvbbit-capability-bge-reranker-base:9.9.9"
    )

    gemma = by_id["llm/gemma-4-12b-it-vllm"]
    assert gemma["catalog_entry"]["runtime_image"] == "vllm/vllm-openai:latest"
    assert gemma["capability_manifest"]["runtime"]["image"] == "vllm/vllm-openai:latest"
    assert gemma["catalog_entry"]["provider_name"] == "gemma_4_12b_it"
    assert gemma["catalog_entry"]["provider_model"] == "google/gemma-4-12B-it"


def test_mcp_gateway_pack_is_self_contained():
    pack_dir = PACKS / "runtimes" / "mcp-gateway"
    required = {
        "rvbbit-pack.yaml",
        "capability.yaml",
        "Dockerfile",
        "main.py",
        "requirements.txt",
        "mcp-test-server/main.py",
    }
    missing = [
        rel
        for rel in sorted(required)
        if not (pack_dir / rel).exists()
    ]
    assert not missing, "mcp-gateway pack is missing files: " + ", ".join(missing)


def test_uber_compose_bootstraps_baseline_capabilities():
    compose_path = ROOT / "docker" / "docker-compose.uber.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"postgres", "lens", "warren", "bootstrap"} <= set(services)
    bootstrap_env = services["bootstrap"]["environment"]
    assert bootstrap_env["RVBBIT_UBER_BOOTSTRAP_CAPABILITIES"] == (
        "${RVBBIT_UBER_BOOTSTRAP_CAPABILITIES:-"
        "smoke/warren-echo,runtimes/python-runtime,runtimes/mcp-gateway,"
        "data/dlt-mirror}"
    )
    assert services["bootstrap"]["command"] == ["rvbbit-uber-bootstrap"]

    postgres_health = services["postgres"]["healthcheck"]["test"]
    assert postgres_health[0] == "CMD-SHELL"
    assert "grep -qx postgres /proc/1/comm" in postgres_health[1]
    assert "pg_isready" in postgres_health[1]

    socket_mount = "${RVBBIT_DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock"
    for service_name in ("lens", "warren"):
        service = services[service_name]
        assert service["environment"]["DOCKER_HOST"] == (
            "${RVBBIT_DOCKER_HOST:-unix:///var/run/docker.sock}"
        )
        assert socket_mount in service["volumes"]

    lens = services["lens"]
    assert lens["environment"]["GMEET_SA_KEY_FILE"] == (
        "/run/secrets/gmeet-service-account.json"
    )
    assert lens["environment"]["GMEET_ADMIN_SUBJECT"] == "${GMEET_ADMIN_SUBJECT:-}"
    assert (
        "${GMEET_SA_KEY_FILE:-/dev/null}:/run/secrets/gmeet-service-account.json:ro"
        in lens["volumes"]
    )

    dockerfile = (ROOT / "docker" / "Dockerfile.rvbbit").read_text(encoding="utf-8")
    assert "docker/uber/bootstrap.sh /usr/local/bin/rvbbit-uber-bootstrap" in dockerfile


def test_calliope_compose_is_a_pinned_hosted_full_stack():
    class ComposeLoader(yaml.SafeLoader):
        pass

    ComposeLoader.add_constructor(
        "!reset", lambda loader, node: loader.construct_sequence(node)
    )
    ComposeLoader.add_constructor(
        "!override", lambda loader, node: loader.construct_sequence(node)
    )
    compose_path = ROOT / "docker" / "docker-compose.calliope.yml"
    compose = yaml.load(compose_path.read_text(encoding="utf-8"), Loader=ComposeLoader)
    services = compose["services"]

    assert {
        "postgres",
        "duck",
        "lens",
        "warren",
        "bootstrap",
        "doc-extract",
        "hindsight",
        "hermes",
        "warehouse-mcp",
        "origin",
    } <= set(services)
    assert services["warehouse-mcp"]["profiles"] == []
    assert services["origin"]["profiles"] == []

    lens_env = services["lens"]["environment"]
    assert lens_env["RVBBIT_MODE"] == "burrow"
    assert lens_env["RVBBIT_AUTH_INTERNAL"] == "http://warehouse-mcp:8765"
    assert services["lens"]["ports"] == [
        "${RVBBIT_LENS_PORT:-127.0.0.1:3000}:3000"
    ]
    assert (
        services["warehouse-mcp"]["environment"]["WAREHOUSE_LOGIN_UI"] == "lens"
    )
    assert "${RVBBIT_BURROW_PORT:-127.0.0.1:3001}:80" in services["origin"][
        "ports"
    ]

    postgres_env = services["postgres"]["environment"]
    assert postgres_env["RVBBIT_CLOVER_KEY"].startswith("${RVBBIT_CLOVER_KEY:?")
    assert postgres_env["RVBBIT_CLOVER_INSTALL_SOURCE"] == "shipped"

    bootstrap_env = services["bootstrap"]["environment"]
    assert bootstrap_env["RVBBIT_CLOVER_REQUIRED"] == "true"
    assert bootstrap_env["RVBBIT_CLOVER_VERIFY_REMOTE"] == "true"
    assert bootstrap_env["RVBBIT_CLOVER_INSTALL_SOURCE"] == "shipped"
    assert bootstrap_env["RVBBIT_CLOVER_REQUIRED_MODEL"].endswith(":-calliope}")
    assert bootstrap_env["RVBBIT_HOSTED_SERVICES"] == "true"
    assert bootstrap_env["RVBBIT_UBER_BOOTSTRAP_CAPABILITIES"].endswith(
        ":-runtimes/python-runtime,runtimes/mcp-gateway,data/dlt-mirror}"
    )

    hindsight = services["hindsight"]
    assert "hindsight-api:0.9.0-slim@sha256:" in hindsight["image"]
    assert hindsight["environment"]["HINDSIGHT_API_DATABASE_SCHEMA"] == "hindsight"
    assert hindsight["environment"]["HINDSIGHT_API_LLM_MODEL"].endswith(
        ":-calliope}"
    )
    assert hindsight["environment"]["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"] == "embed"

    hermes = services["hermes"]
    assert hermes["build"]["dockerfile"] == "docker/Dockerfile.calliope-hermes"
    assert "nousresearch/hermes-agent@sha256:" in hermes["build"]["args"][
        "HERMES_BASE_IMAGE"
    ]
    assert hermes["environment"]["API_SERVER_MODEL_NAME"].endswith(":-calliope}")
    assert hermes["environment"]["HINDSIGHT_API_URL"] == "http://hindsight:8888"
    assert hermes["depends_on"]["hindsight"]["condition"] == "service_started"
    assert hermes["depends_on"]["warehouse-mcp"]["condition"] == "service_healthy"
    assert "hermes" not in services["warehouse-mcp"]["depends_on"]
    assert services["warehouse-mcp"]["environment"]["WAREHOUSE_HERMES_URL"] == (
        "http://hermes:8642"
    )

    hermes_dockerfile = (
        ROOT / "docker" / "Dockerfile.calliope-hermes"
    ).read_text(encoding="utf-8")
    assert "git apply --check --include=tools/mcp_tool.py" in hermes_dockerfile
    assert "69ae247cf3dba34a37ab4af8484b96d3559a4fcf" in hermes_dockerfile


def test_hosted_bootstrap_reapplies_shipped_clover_on_existing_volumes():
    bootstrap = (ROOT / "docker" / "uber" / "bootstrap.sh").read_text(
        encoding="utf-8"
    )
    init = (ROOT / "docker" / "init" / "02-install-clover.sh").read_text(
        encoding="utf-8"
    )

    assert "install_managed_clover" in bootstrap
    assert "refreshing managed/clover from the pinned image snapshot" in bootstrap
    assert "pg_read_file('/usr/share/rvbbit/capabilities/catalog.json')" in bootstrap
    assert "capabilities/packs/managed/clover/capability.json" in bootstrap
    assert "rvbbit.upsert_capability_catalog_entry" in bootstrap
    postgres_dockerfile = (ROOT / "docker" / "Dockerfile.rvbbit").read_text(
        encoding="utf-8"
    )
    assert "chmod -R a+rX /usr/share/rvbbit/capabilities" in postgres_dockerfile
    assert "jsonb_array_elements_text(c.manifest #> '{managed,install,sql}')" in bootstrap
    assert "rvbbit.backend_probe('embed')" in bootstrap
    assert 'RVBBIT_CLOVER_REQUIRED_MODEL:-' in bootstrap
    assert "configure_hosted_clover_defaults" in bootstrap
    assert "rvbbit.bind_extract_entities_to_clover()" in bootstrap
    assert "rvbbit.extract_entities(text,text,jsonb)" in bootstrap
    assert "prepare_hosted_services" in bootstrap
    assert "CREATE EXTENSION IF NOT EXISTS vector" in bootstrap
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public" in bootstrap
    assert "ALTER EXTENSION pg_trgm SET SCHEMA public" in bootstrap
    assert "rvbbit.register_memory_service" in bootstrap
    assert 'RVBBIT_CLOVER_INSTALL_SOURCE:-live' in init
    assert 'CLOVER_INSTALL_SOURCE" = "shipped"' in init


def test_product_images_and_repository_pin_the_same_rust_toolchain():
    toolchain = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    postgres = (ROOT / "docker" / "Dockerfile.rvbbit").read_text(encoding="utf-8")
    warren = (ROOT / "docker" / "Dockerfile.warren-agent").read_text(encoding="utf-8")

    toolchain_version = re.search(r'^channel = "([0-9]+\.[0-9]+\.[0-9]+)"$', toolchain, re.M)
    postgres_version = re.search(r"--default-toolchain ([0-9]+\.[0-9]+\.[0-9]+)", postgres)
    warren_version = re.search(r"^FROM rust:([0-9]+\.[0-9]+)-", warren, re.M)

    assert toolchain_version
    assert postgres_version
    assert warren_version
    assert toolchain_version.group(1) == postgres_version.group(1)
    assert toolchain_version.group(1).startswith(f"{warren_version.group(1)}.")


def test_release_compose_exposes_configurable_docker_socket_to_capability_runners():
    compose_path = ROOT / "docker" / "docker-compose.release.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]
    socket_mount = "${RVBBIT_DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock"

    for service_name in ("lens", "warren"):
        service = services[service_name]
        assert service["environment"]["DOCKER_HOST"] == (
            "${RVBBIT_DOCKER_HOST:-unix:///var/run/docker.sock}"
        )
        assert socket_mount in service["volumes"]

    lens = services["lens"]
    assert lens["environment"]["GMEET_SA_KEY_FILE"] == (
        "/run/secrets/gmeet-service-account.json"
    )
    assert lens["environment"]["GMEET_ADMIN_SUBJECT"] == "${GMEET_ADMIN_SUBJECT:-}"
    assert (
        "${GMEET_SA_KEY_FILE:-/dev/null}:/run/secrets/gmeet-service-account.json:ro"
        in lens["volumes"]
    )
