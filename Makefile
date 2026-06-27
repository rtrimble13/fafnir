# fafnir Makefile

.PHONY: help build install test test-int fmt lint dist clean db-up migrate seed

help:
	@echo "Targets:"
	@echo "  build      Install package in editable mode with dev extras"
	@echo "  install    Install package (non-editable)"
	@echo "  test       Run unit tests (no database required)"
	@echo "  test-int   Run integration tests (needs FAFNIR_TEST_DSN)"
	@echo "  fmt        Format + lint (black, isort, flake8)"
	@echo "  lint       Lint only (check, no changes)"
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

migrate:
	fafnir db migrate

seed:
	fafnir db seed

dist:
	python -m build

clean:
	rm -rf build dist *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
