VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: venv install test lint fmt doctor clean

venv:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(VENV)/bin/ruff check src tests

fmt:
	$(VENV)/bin/ruff check --fix src tests

doctor:
	$(VENV)/bin/orc doctor

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache .ruff_cache
