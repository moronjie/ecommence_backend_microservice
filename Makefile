.PHONY: install up down logs test lint format

PY ?= $(CURDIR)/.venv/Scripts/python

install:
	$(PY) -m pip install -e "libs/common[dev]" -e "services/_template[dev]" -e "services/gateway[dev]" ruff mypy

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

lint:
	$(PY) -m ruff check .
	$(PY) -m mypy libs/common/ecom_common services/gateway/app

format:
	$(PY) -m ruff format .
