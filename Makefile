.PHONY: install up down logs test lint format

install:
	uv venv --python 3.12
	uv pip install -e "libs/common[dev]" -e "services/_template[dev]" ruff mypy

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

test:
	.venv/Scripts/python -m pytest libs/common/tests
	cd services/_template && ../../.venv/Scripts/python -m pytest

lint:
	.venv/Scripts/ruff check .
	.venv/Scripts/mypy libs/common/ecom_common

format:
	.venv/Scripts/ruff format .
