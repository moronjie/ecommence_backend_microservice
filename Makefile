.PHONY: install up down logs test lint format

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

format:
	$(PY) -m ruff format .
