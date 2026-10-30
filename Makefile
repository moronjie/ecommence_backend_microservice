.PHONY: install up down logs test lint format e2e migrations-check keys loadtest

PY ?= $(CURDIR)/.venv/Scripts/python

install:
	$(PY) -m pip install -e "libs/common[dev]" -r requirements-dev.txt

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

test:
	$(PY) -m pytest libs/common/tests
	cd services/_template && $(PY) -m pytest
	cd services/gateway && $(PY) -m pytest
	cd services/auth && $(PY) -m pytest
	cd services/user && $(PY) -m pytest
	cd services/product && $(PY) -m pytest
	cd services/inventory && $(PY) -m pytest
	cd services/cart && $(PY) -m pytest
	cd services/order && $(PY) -m pytest
	cd services/payment && $(PY) -m pytest
	cd services/notification && $(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m mypy libs/common/ecom_common
	cd services/gateway && $(PY) -m mypy app
	cd services/auth && $(PY) -m mypy app
	cd services/user && $(PY) -m mypy app
	cd services/product && $(PY) -m mypy app
	cd services/inventory && $(PY) -m mypy app
	cd services/cart && $(PY) -m mypy app
	cd services/order && $(PY) -m mypy app
	cd services/payment && $(PY) -m mypy app
	cd services/notification && $(PY) -m mypy app

format:
	$(PY) -m ruff format .

# --- checks that need Docker (the stack must be running: make up) ---
# Real purchase through every service, Kafka included. Needs ADMIN_EMAIL / ADMIN_PASSWORD to have been
# set when the stack was started, and the same values here.
e2e:
	E2E_BASE_URL=http://localhost:8000 E2E_ADMIN_EMAIL=$(ADMIN_EMAIL) E2E_ADMIN_PASSWORD=$(ADMIN_PASSWORD) $(PY) -m pytest tests/e2e -v

# Every service's migrations on real Postgres (set POSTGRES_PORT=5433 if you moved it)
POSTGRES_PORT ?= 5432
migrations-check:
	$(PY) scripts/check_migrations.py --port $(POSTGRES_PORT)

# Creates the RS256 signing keys and prints the .env lines for production
keys:
	$(PY) scripts/generate_keys.py

# 30 seconds of 20 shoppers. Start the stack with RATE_LIMIT_REQUESTS=1000000 first.
loadtest:
	$(PY) loadtest/run.py --admin-email $(ADMIN_EMAIL) --admin-password $(ADMIN_PASSWORD) --users 20 --duration 30

# Rebuilds docs/postman/shop.postman_collection.json from the running gateway
postman:
	$(PY) scripts/make_postman_collection.py
