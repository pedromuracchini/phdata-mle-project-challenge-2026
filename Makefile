.PHONY: test-build test-unit test-integration test-all clean help \
	setup test test-hooks lint typecheck bench docker-build docker-run

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

# Hook tests, then the existing unit tests in Docker.
test: test-hooks test-unit
	@if [ ! -f "$(CHARACTERIZATION_TEST)" ]; then \
		echo "notice: $(CHARACTERIZATION_TEST) does not exist yet (added in Phase 1)."; \
		echo "notice: predictions for complete requests are not checked against a baseline."; \
	fi

# Tests for the Claude Code hooks; standard library only, runs on the host.
test-hooks:
	$(call require,python3)
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .claude/hooks -v

lint:
	$(call require,ruff)
	ruff check .
	ruff format --check .

typecheck:
	$(call require,mypy)
	mypy src .claude/hooks

bench:
	@echo "error: no benchmark yet. scripts/benchmark.py is added in Phase 4." >&2
	@exit 1

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
	@echo "  test              - Run hook tests and unit tests"
	@echo "  test-hooks        - Run tests for the Claude Code hooks"
	@echo "  lint              - Run ruff lint and format check"
	@echo "  typecheck         - Run mypy"
	@echo "  bench             - Run the benchmark (not available until Phase 4)"
	@echo "  docker-build      - Build the API image"
	@echo "  docker-run        - Run the API on port $(PORT)"
	@echo ""
	@echo "Examples:"
	@echo "  make test-unit              # Quick unit tests"
	@echo "  make test-integration       # Full integration tests"
	@echo "  make test-all               # Complete test suite"
	@echo "  make clean                  # Clean up after tests"
