# Everyday commands. `make` lists them.
.DEFAULT_GOAL := help
-include .env
export

PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff

help: ## List the commands
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Create .venv, install dependencies and create .env if missing
	python3.12 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt
	@test -f .env || cp .env.example .env

db: ## Start Postgres and apply migrations
	docker compose up -d --wait db
	$(PYTHON) -m scripts.migrate

seed: ## Add made-up listings and a demo login (never run against production)
	$(PYTHON) -m scripts.seed_demo

run: ## Run the site with auto-reload at http://localhost:8000
	.venv/bin/uvicorn app.main:app --reload

test: ## Run the Python tests and the database constraint tests
	docker compose up -d --wait db
	$(PYTHON) -m pytest
	docker compose exec -T db sh -c 'psql -q -U "$$POSTGRES_USER" -d listing_ads_test -v ON_ERROR_STOP=1' < tests/test_schema.sql

lint: ## Check style and formatting
	$(RUFF) check .
	$(RUFF) format --check .

format: ## Fix style and formatting
	$(RUFF) check --fix .
	$(RUFF) format .

lock: ## Regenerate requirements.txt from requirements.in
	uv pip compile --universal --python-version 3.12 requirements.in -o requirements.txt

.PHONY: help setup db seed run test lint format lock
