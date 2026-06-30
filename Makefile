PYTHON ?= python
RUFF ?= ruff

.PHONY: check format format-check lint

lint:
	$(RUFF) check src

format:
	$(RUFF) format src

format-check:
	$(RUFF) format --check src

check: lint format-check
	$(PYTHON) -m compileall -q src/mossy
