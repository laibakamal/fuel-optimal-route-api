# One-command setup. `make setup` takes a clean clone to a working API.
#
# Everything below works with no Docker, no PostgreSQL and no network access
# beyond PyPI: the resolved stop coordinates are committed, so the expensive
# offline pipeline never has to run on a reviewer's machine.

PYTHON ?= python3.13
VENV   := .venv
BIN    := $(VENV)/bin
PORT   ?= 8000

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "make setup    install dependencies, migrate and seed  (run this first)"
	@echo "make run      start the API and map page on :$(PORT)"
	@echo "make test     run the test suite with coverage"
	@echo "make bench    measure p50/p95 latency over 12 real routes"
	@echo "make verify   validate the seeded data"
	@echo "make pipeline regenerate the stop artefact from the CSV (not needed)"
	@echo "make clean    remove the venv and the local database"

$(BIN)/python:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
	  echo "ERROR: $(PYTHON) not found. Django 6.1 requires Python >= 3.12."; \
	  echo "  macOS:  brew install python@3.13"; \
	  echo "  Ubuntu: sudo apt install python3.13 python3.13-venv"; \
	  echo "  Or override: make setup PYTHON=python3.12"; \
	  exit 1; }
	$(PYTHON) -m venv $(VENV)

.PHONY: setup
setup: $(BIN)/python
	$(BIN)/python -m pip install --upgrade pip --quiet
	$(BIN)/python -m pip install -r requirements-dev.txt --quiet
	$(BIN)/python manage.py migrate
	$(BIN)/python manage.py seed_stops
	@echo
	@$(BIN)/django-admin --version | sed 's/^/Django /'
	@echo "Setup complete. Start the server with:  make run"

.PHONY: run
run:
	$(BIN)/python manage.py runserver $(PORT)

.PHONY: test
test:
	$(BIN)/python -m pytest tests/ -q --cov --cov-report=term-missing

.PHONY: bench
bench:
	$(BIN)/python manage.py benchmark --repeats 10 --compare-naive

.PHONY: verify
verify:
	$(BIN)/python manage.py verify_stops

.PHONY: pipeline
pipeline:
	$(BIN)/python manage.py build_fuel_index

.PHONY: clean
clean:
	rm -rf $(VENV) db.sqlite3 .coverage staticfiles
