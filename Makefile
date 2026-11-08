.PHONY: test-build test-unit test-integration test-all clean help \
	setup test test-hooks test-docker lint typecheck bench docker-build docker-run \
	fit-imputer check-imputer evaluate-imputation ab-bench

# Build the test Docker image
test-build:
	docker build -f Dockerfile.test -t ml-api-test .

# Run test containers as the host user so files written to test-results/ stay yours.
HOST_USER := $(shell id -u):$(shell id -g)

# Run unit tests only (fast, no Docker Compose)
test-unit: test-build
	docker run --rm \
		--user $(HOST_USER) \
		-v $(PWD)/test-results:/app/test-results \
		ml-api-test pytest tests/unit -v

# Run integration tests with Docker Compose
test-integration: check-imputer
	HOST_USER=$(HOST_USER) docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from test

# Run all tests (unit + integration)
test-all: check-imputer test-build
	@echo "Running integration tests..."
	HOST_USER=$(HOST_USER) docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from test
	@echo "Running unit tests..."
	docker run --rm \
		--user $(HOST_USER) \
		-v $(PWD)/test-results:/app/test-results \
		ml-api-test pytest tests/unit -v

# Clean up containers and test artifacts
clean:
	docker compose -f docker-compose.test.yml down -v
	rm -rf test-results/*

# ---------------------------------------------------------------------------
# Standard targets used by CLAUDE.md and CI. Everything runs through uv against
# uv.lock (--frozen: fail instead of silently re-resolving), so the host and the
# Docker images use the same versions.
# ---------------------------------------------------------------------------

IMAGE ?= mle-project-challenge-2026
CONTAINER ?= housing-api
PORT ?= 8000
CHARACTERIZATION_TEST ?= tests/test_characterization.py

# Fail with a clear message when a host tool is missing, instead of a shell error.
require = @command -v $(1) >/dev/null 2>&1 || { \
	echo "error: '$(1)' not found on PATH. Install it, then re-run 'make $@'." >&2; \
	exit 1; }

UV_RUN = uv run --frozen

# Install Python (.python-version) and the locked runtime + dev dependencies into .venv.
setup:
	$(call require,uv)
	uv sync --frozen

# Hook tests, unit tests and the characterization test, on the host through uv.
test: test-hooks
	$(call require,uv)
	$(UV_RUN) pytest tests/unit $(CHARACTERIZATION_TEST)

# Characterization test inside the test image: complete requests must reproduce
# tests/fixtures/baseline_predictions.json with the image's (locked) library versions.
test-docker: test-build
	docker run --rm \
		--user $(HOST_USER) \
		-v $(PWD)/test-results:/app/test-results \
		ml-api-test pytest $(CHARACTERIZATION_TEST)

# Tests for the Claude Code hooks (standard library only).
test-hooks:
	$(call require,uv)
	PYTHONDONTWRITEBYTECODE=1 $(UV_RUN) python -m unittest discover -s .claude/hooks -v

lint:
	$(call require,uv)
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

# Keep in sync with the mypy hook in .pre-commit-config.yaml (same command, same files).
TYPECHECK_PATHS = src .claude/hooks scripts tests
typecheck:
	$(call require,uv)
	$(UV_RUN) mypy $(TYPECHECK_PATHS)

# Benchmark a service that is already running (e.g. `make docker-run` in another terminal).
# Fixed load profile: see scripts/benchmark.py. Results land in bench-results/ (gitignored);
# copy the ones worth keeping to docs/benchmarks/. The client is standard library only.
BENCH_URL ?= http://localhost:$(PORT)
# BENCH_PROFILE: complete (default) | one-null | one-null-knn | zipcode-only (see benchmark.py).
BENCH_PROFILE ?= complete
BENCH_OUTPUT ?= bench-results/bench-$(BENCH_PROFILE)-$(shell date -u +%Y%m%dT%H%M%SZ).json
bench:
	$(call require,uv)
	@mkdir -p $(dir $(BENCH_OUTPUT))
	$(UV_RUN) python scripts/benchmark.py --base-url $(BENCH_URL) --profile $(BENCH_PROFILE) --output $(BENCH_OUTPUT)

# Interleaved A/B benchmark (Phase 4 method, scripts/ab_bench.py): builds CONTROL and
# CANDIDATE (a git ref, or `worktree` for the working tree as it is), then runs them A,B / B,A /
# A,B with a fresh container per run, server pinned to AB_SERVER_CPUS and the client to
# AB_CLIENT_CPUS. AB_CONTROL_ARGS / AB_CANDIDATE_ARGS add `docker run` options to one arm.
# Server layout: all CPUs (0-15) until the BLAS/OpenMP threads are limited, because pinning
# main to fewer CPUs makes its OpenMP pool ~2x slower (docs/benchmarks/phase-4-performance.md);
# AB_SERVER_CPUS=0-11 (client-isolated) from the worker sweep on.
CONTROL ?= main
CANDIDATE ?= worktree
AB_REQUESTS ?= 500
AB_ROUNDS ?= 3
AB_PROFILES ?= complete one-null-knn zipcode-only
AB_SERVER_CPUS ?= 0-15
AB_CLIENT_CPUS ?= 12-15
AB_CONTROL_ARGS ?=
AB_CANDIDATE_ARGS ?=
AB_LABEL ?=
AB_OUTPUT ?= bench-results/ab-$(shell date -u +%Y%m%dT%H%M%SZ).json
ab-bench:
	$(call require,uv)
	$(call require,docker)
	$(UV_RUN) python scripts/ab_bench.py --control $(CONTROL) --candidate $(CANDIDATE) \
		--requests $(AB_REQUESTS) --rounds $(AB_ROUNDS) --profiles $(AB_PROFILES) \
		--server-cpus $(AB_SERVER_CPUS) --client-cpus $(AB_CLIENT_CPUS) \
		--control-args "$(AB_CONTROL_ARGS)" --candidate-args "$(AB_CANDIDATE_ARGS)" \
		--label "$(AB_LABEL)" --output $(AB_OUTPUT)

# The KNN imputer artifact is committed (like model.pkl) and baked into the image. Fit it once
# in the locked environment; check-imputer refits in memory and fails on any difference.
fit-imputer:
	$(call require,uv)
	$(UV_RUN) python scripts/fit_imputer.py

check-imputer:
	$(call require,uv)
	$(UV_RUN) python scripts/fit_imputer.py --check

# Imputation evaluation (about 6 minutes): writes docs/imputation/evaluation_results.json and
# evaluation_tables.md; the analysis is docs/imputation_report.md.
evaluate-imputation:
	$(call require,uv)
	$(UV_RUN) python scripts/evaluate_imputation.py

# The image bakes src/model/imputer.joblib in: refuse to build without a valid artifact.
docker-build: check-imputer
	docker build -t $(IMAGE) .

# Runs in the foreground; Ctrl+C stops and removes the container.
docker-run:
	docker run --rm -p $(PORT):8000 --name $(CONTAINER) $(IMAGE)

# Display help information
help:
	@echo "Docker-Based Testing Makefile"
	@echo ""
	@echo "Available targets:"
	@echo "  test-build        - Build the test Docker image"
	@echo "  test-unit         - Run unit tests only (fast)"
	@echo "  test-integration  - Run integration tests with Docker Compose"
	@echo "  test-all          - Run all tests (unit + integration)"
	@echo "  clean             - Remove containers and test artifacts"
	@echo "  help              - Display this help message"
	@echo ""
	@echo "Standard targets:"
	@echo "  setup             - Install Python and locked dependencies with uv"
	@echo "  test              - Run hook, unit and characterization tests (uv)"
	@echo "  test-hooks        - Run tests for the Claude Code hooks"
	@echo "  test-docker       - Run the characterization test in Docker"
	@echo "  lint              - Run ruff lint and format check"
	@echo "  typecheck         - Run mypy"
	@echo "  bench             - Benchmark the running API (BENCH_URL=$(BENCH_URL))"
	@echo "  ab-bench          - Interleaved A/B benchmark: CONTROL=<ref> vs CANDIDATE=<ref|worktree>"
	@echo "  fit-imputer       - Fit the KNN imputer and write src/model/imputer.joblib"
	@echo "  check-imputer     - Refit in memory; fail if the committed imputer differs"
	@echo "  evaluate-imputation - Compare imputation methods (docs/imputation/)"
	@echo "  docker-build      - Build the API image (runs check-imputer first)"
	@echo "  docker-run        - Run the API on port $(PORT)"
	@echo ""
	@echo "Examples:"
	@echo "  make test-unit              # Quick unit tests"
	@echo "  make test-integration       # Full integration tests"
	@echo "  make test-all               # Complete test suite"
	@echo "  make clean                  # Clean up after tests"
