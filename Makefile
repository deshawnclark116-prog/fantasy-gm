PY ?= .venv/bin/python

.PHONY: install lint typecheck test test-pg check migrate lock

install:
	uv sync --frozen --all-extras

lock:
	uv lock

lint:
	$(PY) -m ruff check src tests alembic
	$(PY) -m ruff format --check src tests alembic

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

# Requires a PostgreSQL server, e.g.
# FANTASY_GM_TEST_POSTGRES_URL=postgresql+psycopg://postgres@localhost:5432/fantasy_gm_test
test-pg:
	FANTASY_GM_REQUIRE_POSTGRES=1 $(PY) -m pytest -m postgres

check: lint typecheck test

migrate:
	$(PY) -m alembic upgrade head
