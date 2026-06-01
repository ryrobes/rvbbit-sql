COMPOSE := docker compose -f docker/docker-compose.yml
COMPOSE_SIDECARS := docker compose -f docker/docker-compose.yml -f docker/docker-compose.sidecars.yml
RVBBIT_VERSION ?= $(shell awk -F'"' '/^version[[:space:]]*=/ {print $$2; exit}' Cargo.toml)
RELEASE_VERSION ?= $(RVBBIT_VERSION)
IMAGE_NAMESPACE ?= ryrobes
PYTHON_RUNTIME_IMAGE ?= rvbbit/python-runtime:local
MCP_GATEWAY_IMAGE ?= rvbbit/mcp-gateway:local

.PHONY: help build up down logs psql-heap psql-rvbbit bench-shell info clean \
        reload-extension e2e-realworld e2e-realworld-fresh e2e-realworld-live \
        e2e-realworld-warren \
        gpu-up gpu-down register-specialists \
        python-runtime-image python-runtime-up mcp-gateway-image mcp-gateway-up restore-local-embed gpu-status \
        bigfoot-kg-demo capabilities-list capability-render capability-catalog \
        capability-catalog-seed capability-catalog-db \
        capability-scaffold capability-install capability-deploy capability-test \
        capability-test-all warren-agent warren-once \
        release-bump release-build release-push release-compose-up

RVBBIT_DSN ?= postgresql://postgres:rvbbit@localhost:55433/bench
WARREN_NODE ?= local-warren
WARREN_WORK_DIR ?= .rvbbit/warren
WARREN_LABELS ?= {"capability":true,"docker":true,"gpu":false}
WARREN_CAPACITY ?= {}
WARREN_DOCKER_NETWORK ?= docker_default
WARREN_METRICS_MS ?= 10000
CAPABILITY_TEST_VISIBILITY ?= public
CAPABILITY_TEST_OUT ?= .rvbbit/capability-acceptance

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build:           ## Build the rvbbit + bench images
	$(COMPOSE) build

up:              ## Start heap baseline + rvbbit + bench (builds if needed)
	$(COMPOSE) up -d --build

down:            ## Stop everything (keeps volumes)
	$(COMPOSE) down

nuke:            ## Stop and delete volumes (wipes both databases)
	$(COMPOSE) down -v

logs:            ## Tail logs from all services
	$(COMPOSE) logs -f

logs-rvbbit:     ## Tail rvbbit container logs only
	$(COMPOSE) logs -f pg-rvbbit

psql-heap:       ## psql into the heap baseline
	$(COMPOSE) exec pg-heap psql -U postgres -d bench

psql-rvbbit:     ## psql into the rvbbit instance
	$(COMPOSE) exec pg-rvbbit psql -U postgres -d bench

bench-shell:     ## Shell in the bench container
	$(COMPOSE) exec bench bash

info:            ## Print versions from both servers via the bench runner
	$(COMPOSE) exec bench python run.py info

smoke:           ## Phase 1a smoke test: CREATE / INSERT / SELECT via rvbbit AM
	$(COMPOSE) exec bench python run.py smoke

load-llm:        ## Load N rows of LLM-shaped synthetic data (defaults to 100k)
	$(COMPOSE) exec bench python run.py load llm --rows $${ROWS:-100000}

compact-llm:     ## Run rvbbit.compact() on the loaded llm_events table
	$(COMPOSE) exec bench python run.py compact

query-llm:       ## Pair-wise compare heap vs rvbbit on the LLM query set
	$(COMPOSE) exec bench python run.py query llm

test:            ## Run E2E tests (skips live-LLM ones; cheap & deterministic)
	$(MAKE) --no-print-directory python-runtime-up
	$(MAKE) --no-print-directory mcp-gateway-up
	$(COMPOSE) exec -T bench pytest /tests -x

test-live:       ## Run E2E tests INCLUDING live LLM calls (costs $$)
	$(MAKE) --no-print-directory python-runtime-up
	$(MAKE) --no-print-directory mcp-gateway-up
	$(COMPOSE) exec -T -e RUN_LLM_TESTS=1 bench pytest /tests

e2e-realworld:   ## Run the real-world acceptance harness (deterministic/default)
	$(COMPOSE_SIDECARS) up -d --build pg-rvbbit pg-heap bench echo echo-openai-embed
	$(MAKE) --no-print-directory python-runtime-up
	$(MAKE) --no-print-directory mcp-gateway-up
	$(COMPOSE) exec -T bench python /bench/e2e_realworld.py

e2e-realworld-fresh: ## Destructive fresh acceptance run (deletes Docker volumes)
	$(COMPOSE_SIDECARS) down -v
	$(MAKE) --no-print-directory e2e-realworld

e2e-realworld-live: ## Run acceptance harness with live provider calls enabled
	$(COMPOSE_SIDECARS) up -d --build pg-rvbbit pg-heap bench echo echo-openai-embed
	$(MAKE) --no-print-directory python-runtime-up
	$(MAKE) --no-print-directory mcp-gateway-up
	$(COMPOSE) exec -T -e RVBBIT_E2E_LIVE_LLM=1 bench python /bench/e2e_realworld.py

e2e-realworld-warren: ## Run real Warren deploy/probe/operator acceptance smoke
	$(COMPOSE_SIDECARS) up -d --build pg-rvbbit pg-heap bench echo echo-openai-embed
	$(MAKE) --no-print-directory reload-extension
	@JOB_NAME=e2e-warren-smoke-$$(date +%Y%m%d%H%M%S); \
	  echo "queueing $$JOB_NAME"; \
	  capabilities/tools/rvbbit-capability deploy capabilities/packs/smoke/warren-echo \
	    --dsn '$(RVBBIT_DSN)' \
	    --target '{"capability":true,"docker":true,"gpu":false}' \
	    --job-name "$$JOB_NAME"; \
	  cargo run -p warren-agent -- \
	    --dsn '$(RVBBIT_DSN)' \
	    --node 'e2e-warren-local' \
	    --work-dir '.rvbbit/warren-e2e' \
	    --docker-network '$(WARREN_DOCKER_NETWORK)' \
	    --labels '{"capability":true,"docker":true,"gpu":false}' \
	    --capacity '{"e2e":true}' \
	    --metrics-ms 1000 \
	    --once
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench -P pager=off -v ON_ERROR_STOP=1 \
	  < docker/sql/e2e-warren-verify.sql

reload-extension: ## Non-destructive extension reload/update; preserves KG/cache/router data
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 \
	  -c "CREATE EXTENSION IF NOT EXISTS pg_rvbbit;" \
	  -c "ALTER EXTENSION pg_rvbbit UPDATE;"

bigfoot-load:    ## Load BFRO sightings CSV into rvbbit
	$(COMPOSE) exec bench python /bench/bigfoot_bench.py load

bigfoot-bench:   ## Benchmark the user-style semantic query (LIMIT=N to size; default 20)
	$(COMPOSE) exec -e LIMIT=$${LIMIT:-20} bench python /bench/bigfoot_bench.py run

# ---- GPU specialist sidecars ---------------------------------------------
#
# These are the "real model" backends — BGE-M3 embeddings, BGE reranker
# (via Gradio), GLiNER (extract). They're profile-gated so a plain
# `make up` won't pull them in. First boot downloads ~5-7GB of weights.

gpu-up:          ## Start the GPU sidecars (embed / rerank / extract) + main stack
	$(COMPOSE_SIDECARS) --profile models up -d --build

gpu-down:        ## Stop the GPU sidecars (keeps the HF cache volume)
	$(COMPOSE_SIDECARS) --profile models down

gpu-status:      ## Show health of the GPU sidecars
	@$(COMPOSE_SIDECARS) ps embed rerank extract 2>/dev/null || true
	@echo "--- /health ---"
	@for svc in embed:8091 rerank:8093/config extract:8094; do \
	  printf "%-10s " $${svc%%:*}; \
	  port=$$(echo $${svc#*:} | cut -d/ -f1); \
	  curl -sS --max-time 2 http://localhost:$${port}/health 2>&1 || echo "down"; \
	done

register-specialists:  ## Register GPU specialists in rvbbit AND wire operators to use them (idempotent; replaces embed)
	$(COMPOSE_SIDECARS) exec -T pg-rvbbit psql -U postgres -d bench \
	  -v ON_ERROR_STOP=1 \
	  < docker/sql/register-gpu-specialists.sql
	@$(MAKE) --no-print-directory wire-specialists

restore-local-embed:  ## Restore the default local CPU embed backend after GPU demos/tests
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench \
	  -v ON_ERROR_STOP=1 \
	  < docker/sql/register-local-embed.sql

wire-specialists:  ## (Re)wire the LLM operators to route through the GPU specialists
	$(COMPOSE_SIDECARS) exec -T pg-rvbbit psql -U postgres -d bench \
	  -v ON_ERROR_STOP=1 \
	  < docker/sql/wire-operators-to-specialists.sql

python-runtime-image: ## Build an optional local Python runtime OCI image
	docker build -t '$(PYTHON_RUNTIME_IMAGE)' sidecars/python-runtime

python-runtime-up: ## Deploy/register the built-in Python runtime through Warren
	$(COMPOSE) up -d --build pg-rvbbit pg-heap bench
	$(MAKE) --no-print-directory reload-extension
	@docker rm -f rvbbit-python-runtime >/dev/null 2>&1 || true
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench \
	  -v ON_ERROR_STOP=1 \
	  < docker/sql/deploy-python-runtime.sql
	$(MAKE) --no-print-directory warren-once
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench -P pager=off \
	  -v ON_ERROR_STOP=1 \
	  -c "SELECT name, endpoint_url, status, runtime_source FROM rvbbit.python_runtimes WHERE name = 'python_default';"

mcp-gateway-image: ## Build an optional local MCP Gateway OCI image
	docker build -t '$(MCP_GATEWAY_IMAGE)' capabilities/packs/runtimes/mcp-gateway

mcp-gateway-up: ## Deploy/register the built-in MCP Gateway runtime through Warren
	$(COMPOSE) up -d --build pg-rvbbit pg-heap bench
	$(MAKE) --no-print-directory reload-extension
	@docker rm -f rvbbit-mcp-gateway >/dev/null 2>&1 || true
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench \
	  -v ON_ERROR_STOP=1 \
	  < docker/sql/deploy-mcp-gateway.sql
	$(MAKE) --no-print-directory warren-once
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench -P pager=off \
	  -v ON_ERROR_STOP=1 \
	  -c "SELECT name, endpoint_url, status, gateway_source FROM rvbbit.mcp_gateways WHERE name = 'mcp_default';"

bigfoot-demo:    ## Run the bigfoot demo (self-registers + wires specialists; only needs `make gpu-up` + `make bigfoot-load` first)
	cat docker/sql/register-gpu-specialists.sql \
	    docker/sql/wire-operators-to-specialists.sql \
	    docker/sql/bigfoot-demo.sql \
	  | $(COMPOSE_SIDECARS) exec -T pg-rvbbit psql -U postgres -d bench -P pager=off -v ON_ERROR_STOP=1

bigfoot-kg-demo: ## Build/query a deterministic KG over BFRO observations (no GPU/LLM calls; needs `make bigfoot-load`)
	$(COMPOSE) exec -T pg-rvbbit psql -U postgres -d bench \
	  -P pager=off -v ON_ERROR_STOP=1 \
	  < docker/sql/bigfoot-kg-demo.sql

capabilities-list: ## List curated Rvbbit backend/operator capability packs
	capabilities/tools/rvbbit-capability list

capability-render: ## Render a capability pack or manifest (MANIFEST=capabilities/packs/...)
	@test -n "$${MANIFEST:-}" || (echo "set MANIFEST=capabilities/packs/..." >&2; exit 2)
	capabilities/tools/rvbbit-capability render "$${MANIFEST}"

capability-catalog: ## Rebuild capabilities/catalog.json for UI browsing
	capabilities/tools/rvbbit-capability catalog build --output capabilities/catalog.json
	capabilities/tools/rvbbit-capability catalog seed-json --output crates/pg_rvbbit/src/capability_catalog_seed.json

capability-catalog-seed: ## Rebuild extension install seed JSON for rvbbit.capability_catalog
	capabilities/tools/rvbbit-capability catalog seed-json --output crates/pg_rvbbit/src/capability_catalog_seed.json

capability-catalog-db: ## Publish curated capability catalog into rvbbit.capability_catalog
	capabilities/tools/rvbbit-capability catalog publish --dsn '$(RVBBIT_DSN)' --prune

capability-scaffold: ## Scaffold a capability (MANIFEST=... OUT=.rvbbit/capabilities/name)
	@test -n "$${MANIFEST:-}" || (echo "set MANIFEST=capabilities/packs/..." >&2; exit 2)
	capabilities/tools/rvbbit-capability scaffold "$${MANIFEST}" "$${OUT:-.rvbbit/capabilities/$$(basename "$${MANIFEST%.*}")}" --force

capability-install: ## Scaffold/run/register a capability (MANIFEST=..., optional GPU=1, RVBBIT_DSN=...)
	@test -n "$${MANIFEST:-}" || (echo "set MANIFEST=capabilities/packs/..." >&2; exit 2)
	capabilities/tools/rvbbit-capability install "$${MANIFEST}" --force $${GPU:+--gpu}

capability-deploy: ## Queue a capability for Warren (MANIFEST=..., optional TARGET='{"gpu":true}')
	@test -n "$(MANIFEST)" || (echo "set MANIFEST=capabilities/packs/..." >&2; exit 2)
	capabilities/tools/rvbbit-capability deploy "$(MANIFEST)" \
	  --dsn '$(RVBBIT_DSN)' \
	  --target '$(if $(TARGET),$(TARGET),{})' \
	  $(if $(JOB_NAME),--job-name '$(JOB_NAME)',)

capability-test: ## Deploy a capability pack through Warren and run its acceptance SQL
	@test -n "$(MANIFEST)" || (echo "set MANIFEST=capabilities/packs/..." >&2; exit 2)
	capabilities/tools/rvbbit-capability test "$(MANIFEST)" \
	  --dsn '$(RVBBIT_DSN)' \
	  $(if $(TARGET),--target '$(TARGET)',) \
	  --node 'capability-test-warren' \
	  --work-dir '.rvbbit/warren-capability-test' \
	  --docker-network '$(WARREN_DOCKER_NETWORK)' \
	  --labels '$(WARREN_LABELS)' \
	  --capacity '{"capability_test":true}' \
	  --metrics-ms 1000

capability-test-all: ## Sweep selected capability packs through Warren acceptance tests
	capabilities/tools/rvbbit-capability test-all \
	  --dsn '$(RVBBIT_DSN)' \
	  --visibility '$(CAPABILITY_TEST_VISIBILITY)' \
	  --out-dir '$(CAPABILITY_TEST_OUT)' \
	  --node 'capability-test-warren' \
	  --work-dir '.rvbbit/warren-capability-test-all' \
	  --docker-network '$(WARREN_DOCKER_NETWORK)' \
	  --labels '$(WARREN_LABELS)' \
	  --capacity '{"capability_test_all":true}' \
	  --metrics-ms 1000 \
	  $(if $(INCLUDE_GPU),--include-gpu,) \
	  $(if $(ONLY),--only '$(ONLY)',) \
	  $(if $(SKIP),--skip '$(SKIP)',) \
	  $(if $(FAIL_FAST),--fail-fast,)

warren-agent: ## Run a local Warren deployment agent
	cargo run -p warren-agent -- \
	  --dsn '$(RVBBIT_DSN)' \
	  --node '$(WARREN_NODE)' \
	  --work-dir '$(WARREN_WORK_DIR)' \
	  --docker-network '$(WARREN_DOCKER_NETWORK)' \
	  --labels '$(WARREN_LABELS)' \
	  --capacity '$(WARREN_CAPACITY)' \
	  --metrics-ms '$(WARREN_METRICS_MS)'

warren-once: ## Claim at most one Warren job, useful for smoke/debug
	cargo run -p warren-agent -- \
	  --dsn '$(RVBBIT_DSN)' \
	  --node '$(WARREN_NODE)' \
	  --work-dir '$(WARREN_WORK_DIR)' \
	  --docker-network '$(WARREN_DOCKER_NETWORK)' \
	  --labels '$(WARREN_LABELS)' \
	  --capacity '$(WARREN_CAPACITY)' \
	  --metrics-ms '$(WARREN_METRICS_MS)' \
	  --once

release-bump: ## Bump Cargo/control/Lens versions (RELEASE_VERSION=x.y.z)
	scripts/release/bump-version.py '$(RELEASE_VERSION)'

release-build: ## Build release images locally (RELEASE_VERSION=x.y.z)
	scripts/release/build-and-push.sh \
	  --version '$(RELEASE_VERSION)' \
	  --namespace '$(IMAGE_NAMESPACE)'

release-push: ## Build and push release images to GHCR (RELEASE_VERSION=x.y.z)
	scripts/release/build-and-push.sh \
	  --version '$(RELEASE_VERSION)' \
	  --namespace '$(IMAGE_NAMESPACE)' \
	  --push \
	  --tag-latest

release-compose-up: ## Start the published-image clean-slate stack
	RVBBIT_VERSION='$(RELEASE_VERSION)' docker compose -f docker/docker-compose.release.yml up -d

clean:           ## Remove built artifacts
	cargo clean

# ---------------------------------------------------------------------------
# Packaging: extract the built extension + sidecar binary from the docker
# image into a self-contained tarball that ./install.sh can apply on any
# Debian/Ubuntu host with `postgresql-18` installed.
# ---------------------------------------------------------------------------

PG_VERSION ?= 18
PKG_ARCH ?= $(shell dpkg --print-architecture 2>/dev/null || uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')
PKG_NAME := rvbbit-$(RVBBIT_VERSION)-pg$(PG_VERSION)-linux-$(PKG_ARCH)
PKG_DIR  := dist/$(PKG_NAME)
PKG_TAR  := dist/$(PKG_NAME).tar.gz

.PHONY: package package-extract package-clean

package: ## Build the pg-rvbbit image, then emit dist/<name>.tar.gz
	@echo ">>> building docker-pg-rvbbit (needed for artifact extraction)"
	$(COMPOSE) build pg-rvbbit
	$(MAKE) --no-print-directory package-extract

package-extract: ## Extract a tarball from a pre-built docker-pg-rvbbit:latest (no build)
	@echo ">>> extracting artifacts into $(PKG_DIR)/"
	rm -rf $(PKG_DIR)
	mkdir -p $(PKG_DIR)/extension $(PKG_DIR)/lib $(PKG_DIR)/bin
	@cid=$$(docker create docker-pg-rvbbit:latest) && \
	  docker cp $$cid:/usr/share/postgresql/$(PG_VERSION)/extension/. $(PKG_DIR)/extension/ && \
	  docker cp $$cid:/usr/lib/postgresql/$(PG_VERSION)/lib/pg_rvbbit.so $(PKG_DIR)/lib/ && \
	  docker cp $$cid:/usr/local/bin/rvbbit-duck $(PKG_DIR)/bin/ && \
	  docker rm $$cid >/dev/null
	@# Filter out the generic postgres extension files we didn't produce.
	@find $(PKG_DIR)/extension -type f ! -name 'pg_rvbbit*' -delete
	cp docker/init/01-create-extension.sql $(PKG_DIR)/init.sql
	cp install.sh $(PKG_DIR)/install.sh
	cp PACKAGING.md $(PKG_DIR)/README.md
	chmod +x $(PKG_DIR)/install.sh
	@echo "$(RVBBIT_VERSION)" > $(PKG_DIR)/VERSION
	tar -C dist -czf $(PKG_TAR) $(PKG_NAME)
	@echo
	@echo "wrote $(PKG_TAR) ($$(du -h $(PKG_TAR) | cut -f1))"
	@echo "    install with: tar xzf $(PKG_TAR) && cd $(PKG_NAME) && sudo ./install.sh"

package-clean: ## Remove the dist/ packaging staging area
	rm -rf dist/
