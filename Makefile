# fafnir Makefile

.PHONY: help build install test test-int fmt lint dist clean db-up migrate seed version bump release

help:
	@echo "Targets:"
	@echo "  build      Install package in editable mode with dev extras"
	@echo "  install    Install package (non-editable)"
	@echo "  test       Run unit tests (no database required)"
	@echo "  test-int   Run integration tests (needs FAFNIR_TEST_DSN)"
	@echo "  fmt        Format + lint (black, isort, flake8)"
	@echo "  lint       Lint only (check, no changes)"
	@echo "  version    Show the current version, the last tag, and the deployed commit"
	@echo "  bump       Set this branch's version: make bump BUMP=patch|minor|major|X.Y.Z"
	@echo "             (merging the pull request publishes it: doc/releasing.md)"
	@echo "  migrate    Apply migrations to \$$FAFNIR_DSN"
	@echo "  seed       Apply seeds + trading calendar"
	@echo "  dist       Build wheel + sdist"
	@echo "  clean      Remove build artifacts"

build:
	pip install -e .[dev]

install:
	pip install .

test:
	pytest test/ -v -m "not integration"

test-int:
	pytest test/ -v -m integration

fmt:
	black src test
	isort src test
	flake8 src test

lint:
	black --check src test
	isort --check-only src test
	flake8 src test

# One version, in one place. `make version` reports; `make bump` sets the version a
# pull request releases, and merging it publishes the release (ADR 0022). Both are
# scripts/release.sh -- see doc/releasing.md and ADR 0011.
version:
	@scripts/release.sh

bump:
	@test -n "$(BUMP)" || { \
	    echo "Usage: make bump BUMP=patch|minor|major|X.Y.Z"; \
	    echo "       make bump BUMP=minor DRY_RUN=1     # change nothing"; \
	    exit 1; \
	}
	scripts/release.sh $(BUMP) $(if $(DRY_RUN),--dry-run,)

release:
	@echo "A release is the merge of a pull request that raises the version (ADR 0022)."
	@echo "On your branch: make bump BUMP=patch|minor|major, commit, and merge."
	@exit 1

migrate:
	fafnir db migrate

seed:
	fafnir db seed

dist:
	python -m build

clean:
	rm -rf build dist *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
