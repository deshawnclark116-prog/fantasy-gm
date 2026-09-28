PY ?= .venv/bin/python

.PHONY: install lint typecheck test check migrate

install:
	uv venv -p 3.12 .venv && uv pip install -p $(PY) -e ".[dev]"

lint:
	$(PY) -m ruff check src tests alembic
	$(PY) -m ruff format --check src tests alembic

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

check: lint typecheck test

migrate:
	$(PY) -m alembic upgrade head
