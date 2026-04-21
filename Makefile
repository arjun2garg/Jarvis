VENV        = .venv
PYTHON      = $(VENV)/bin/python
PIP         = $(VENV)/bin/pip

.DEFAULT_GOAL := help

.PHONY: help setup verify sync sync-all sync-todoist sync-hevy sync-dry-run sync-active sync-completed sync-hevy-dry-run clean

help:
	@echo ""
	@echo "  Jarvis — available commands"
	@echo ""
	@echo "  make setup              Create .venv, install deps, scaffold .env"
	@echo "  make verify             Check env vars + API connectivity"
	@echo ""
	@echo "  make sync               Run every source (Todoist + Hevy)"
	@echo "  make sync-todoist       Full Todoist sync (active + completed)"
	@echo "  make sync-hevy          Full Hevy workouts sync"
	@echo ""
	@echo "  make sync-dry-run       Preview Todoist sync — no writes"
	@echo "  make sync-hevy-dry-run  Preview Hevy sync — no writes"
	@echo "  make sync-active        Sync active Todoist tasks only"
	@echo "  make sync-completed     Sync completed Todoist tasks only"
	@echo ""
	@echo "  make clean              Remove .venv and caches"
	@echo ""

setup:
	@bash setup.sh

verify: $(VENV)
	$(PYTHON) verify_setup.py

sync: sync-todoist sync-hevy

sync-all: sync

sync-todoist: $(VENV)
	$(PYTHON) sync_todoist.py

sync-hevy: $(VENV)
	$(PYTHON) sync_hevy.py

sync-dry-run: $(VENV)
	$(PYTHON) sync_todoist.py --dry-run

sync-hevy-dry-run: $(VENV)
	$(PYTHON) sync_hevy.py --dry-run

sync-active: $(VENV)
	$(PYTHON) sync_todoist.py --active-only

sync-completed: $(VENV)
	$(PYTHON) sync_todoist.py --completed-only

clean:
	rm -rf $(VENV) __pycache__ *.pyc .pytest_cache .ruff_cache

$(VENV):
	@echo "Run 'make setup' first."
	@exit 1
