.PHONY: test lint fmt
PY := .venv/bin/python

test:  ## free, offline, deterministic (fake model); no cloud calls
	$(PY) -m pytest -q -W ignore

lint:
	.venv/bin/ruff check . --exclude .venv
	.venv/bin/ruff format --check . --exclude .venv

fmt:
	.venv/bin/ruff format . --exclude .venv
