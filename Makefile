.PHONY: test-build test-unit test-integration test-all clean help \
	setup test test-hooks test-docker lint typecheck bench docker-build docker-run

# Build the test Docker image
test-build:
	docker build -f Dockerfile.test -t ml-api-test .

# Run unit tests only (fast, no Docker Compose)
test-unit: test-build
	docker run --rm \
		-v $(PWD)/test-results:/app/test-results \
		ml-api-test pytest tests/unit -v

# Run integration tests with Docker Compose
test-integration:
	docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from test

# Run all tests (unit + integration)
test-all: test-build
	@echo "Running integration tests..."
	docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from test
	@echo "Running unit tests..."
	docker run --rm \
		-v $(PWD)/test-results:/app/test-results \
		ml-api-test pytest tests/unit -v

# Clean up containers and test artifacts
clean:
	docker-compose -f docker-compose.test.yml down -v
	rm -rf test-results/*

# ---------------------------------------------------------------------------
# Standard targets used by CLAUDE.md and CI. Phase 0 runs them on the current
# tooling (pip + Docker); Phase 2 moves them to uv.
# ---------------------------------------------------------------------------

IMAGE ?= mle-project-challenge-2026
CONTAINER ?= housing-api
PORT ?= 8000
CHARACTERIZATION_TEST ?= tests/test_characterization.py

# Fail with a clear message when a host tool is missing, instead of a shell error.
require = @command -v $(1) >/dev/null 2>&1 || { \
	echo "error: '$(1)' not found on PATH. Install it, then re-run 'make $@'." >&2; \
	exit 1; }

# Install dependencies. They live in the Docker test image, not on the host:
# requirements.txt is unpinned, and model.pkl needs the scikit-learn version
# that the image's Python 3.10 resolves to.
setup: test-build

# Hook tests, the unit tests and the characterization test, all but the hooks in Docker.
test: test-hooks test-unit test-docker

# Characterization test: complete requests must reproduce tests/fixtures/baseline_predictions.json.
# Runs in the test image because the baseline was recorded with the image's library versions;
# the host does not have them (and does not have pytest) until Phase 2.
test-docker: test-build
	docker run --rm \
		-v $(PWD)/test-results:/app/test-results \
		ml-api-test pytest $(CHARACTERIZATION_TEST)

# Tests for the Claude Code hooks; standard library only, runs on the host.
test-hooks:
	$(call require,python3)
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .claude/hooks -v

lint:
	$(call require,ruff)
	ruff check .
	ruff format --check .

# Two runs: tests/conftest.py imports `src.main`, which mypy would otherwise also find as
# `main` (mypy_path = src) and reject as the same file under two module names.
typecheck:
	$(call require,mypy)
	mypy src .claude/hooks
	mypy scripts tests

# Benchmark a service that is already running (e.g. `make docker-run` in another terminal).
# Fixed load profile: see scripts/benchmark.py. Results land in bench-results/ (gitignored);
# copy the ones worth keeping to docs/benchmarks/. Standard library only, runs on the host.
BENCH_URL ?= http://localhost:$(PORT)
BENCH_OUTPUT ?= bench-results/bench-$(shell date -u +%Y%m%dT%H%M%SZ).json
bench:
	$(call require,python3)
	@mkdir -p $(dir $(BENCH_OUTPUT))
	python3 scripts/benchmark.py --base-url $(BENCH_URL) --output $(BENCH_OUTPUT)

docker-build:
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
	@echo "  setup             - Install dependencies (builds the Docker test image)"
	@echo "  test              - Run hook, unit and characterization tests"
	@echo "  test-hooks        - Run tests for the Claude Code hooks"
	@echo "  test-docker       - Run the characterization test in Docker"
	@echo "  lint              - Run ruff lint and format check"
	@echo "  typecheck         - Run mypy"
	@echo "  bench             - Benchmark the running API (BENCH_URL=$(BENCH_URL))"
	@echo "  docker-build      - Build the API image"
	@echo "  docker-run        - Run the API on port $(PORT)"
	@echo ""
	@echo "Examples:"
	@echo "  make test-unit              # Quick unit tests"
	@echo "  make test-integration       # Full integration tests"
	@echo "  make test-all               # Complete test suite"
	@echo "  make clean                  # Clean up after tests"
