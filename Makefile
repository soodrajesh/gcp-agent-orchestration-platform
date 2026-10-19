.PHONY: test lint fmt
PY := .venv/bin/python

test:  ## free, offline, deterministic (fake model); no cloud calls
	# the `pytest` console script, exactly as CI runs it (`python -m pytest` would also put the cwd on sys.path)
	.venv/bin/pytest -q -W ignore

lint:
	.venv/bin/ruff check . --exclude .venv
	.venv/bin/ruff format --check . --exclude .venv

fmt:
	.venv/bin/ruff format . --exclude .venv
