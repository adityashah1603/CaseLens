# CaseLens developer commands. Needs GNU make (on Windows, MSYS2 `mingw32-make` works from Git Bash).

ifeq ($(OS),Windows_NT)
  VENV_BIN := .venv/Scripts
else
  VENV_BIN := .venv/bin
endif

PYTHON ?= python
PY     := $(VENV_BIN)/python
WEB    := apps/web

# Load local settings (POSTGRES_*, CASELENS_*) from .env when present.
-include .env
export

.PHONY: help install env up down migrate test test-py test-web lint lint-py lint-web fmt eval eval-full

help:
	@echo "install  create .venv and install Python + web deps"
	@echo "up       start Postgres+pgvector and MinIO (docker compose)"
	@echo "down     stop local services"
	@echo "migrate  apply DB migrations to the dev database"
	@echo "test     Python tests (incl. DB contract tests when CASELENS_TEST_DATABASE_URL is set) + web tests"
	@echo "lint     ruff + mypy --strict + eslint + tsc"
	@echo "fmt      auto-format Python"
	@echo "eval     smoke eval: 10 gold questions, scored (skips without DB / model key)"
	@echo "eval-full  all 50 gold questions, scored, results table written to README.md"

install:
	test -d .venv || $(PYTHON) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt
	cd $(WEB) && pnpm install --frozen-lockfile

env:
	test -f .env || cp .env.example .env

up: env
	docker compose up -d --wait postgres
	docker compose up -d minio minio-init

down:
	docker compose down

migrate:
	cd apps/api && ../../$(PY) -m alembic upgrade head

test: test-py test-web

test-py:
	$(PY) -m pytest

test-web:
	cd $(WEB) && pnpm test

lint: lint-py lint-web

lint-py:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy

lint-web:
	cd $(WEB) && pnpm lint && pnpm typecheck

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

eval:
	$(PY) -m caselens_evals smoke

eval-full:
	$(PY) -m caselens_evals full
