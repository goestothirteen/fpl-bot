.PHONY: help install test test-all lint fmt run up down logs migrate backup smoke

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install:  ## create the venv and install with dev extras
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

test:  ## unit tests (no network)
	.venv/bin/pytest -q

test-all:  ## unit + live integration tests against the real FPL API
	.venv/bin/pytest -q && .venv/bin/pytest -q -m integration --run-integration

lint:  ## ruff
	.venv/bin/ruff check src tests

fmt:  ## ruff format
	.venv/bin/ruff format src tests

smoke:  ## render a real league table to the terminal, no Telegram needed
	.venv/bin/python scripts/smoke.py $(LEAGUE)

run:  ## run the bot locally with long polling
	USE_POLLING=true .venv/bin/python -m fplbot

up:  ## start the full stack
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f bot

migrate:  ## generate a new migration: make migrate M="add thing"
	.venv/bin/alembic revision --autogenerate -m "$(M)"

backup:
	./docker/backup.sh
