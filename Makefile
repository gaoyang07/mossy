PYTHON ?= python
RUFF ?= ruff

.PHONY: check format format-check lint test

lint:
	$(RUFF) check src tests

format:
	$(RUFF) format src tests

format-check:
	$(RUFF) format --check src tests

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

check: lint format-check test
	$(PYTHON) -m compileall -q src/mossy
