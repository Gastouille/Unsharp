# Shorthands for Linux and macOS.  Windows users: run `python install.py`
# and then the `unsharp-bot` commands directly.
.DEFAULT_GOAL := help
VENV    := .venv
PYTHON  := $(VENV)/bin/python
BOT     := $(VENV)/bin/unsharp-bot

.PHONY: help setup dev init check scan run dry-run backtest test lint clean distclean

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Install everything (creates .venv, config.yaml and .env)
	python3 install.py

dev:  ## Install with the test dependencies
	python3 install.py --dev

init:  ## Enter your XTB credentials
	$(BOT) init

check:  ## Validate the configuration and the broker connection
	$(BOT) check

scan:  ## One-shot scan of the current candles, never trades
	$(BOT) scan

dry-run:  ## Run the loop without sending any order
	$(BOT) run --dry-run

run:  ## Run the bot for real (uses XTB_MODE from .env)
	$(BOT) run

backtest:  ## Backtest, e.g. make backtest SYMBOL=US500 DAYS=60
	$(BOT) backtest --symbol $(or $(SYMBOL),US500) --days $(or $(DAYS),30)

test:  ## Run the test suite
	$(PYTHON) -m pytest

lint:  ## Check for unused imports and dead code
	$(PYTHON) -m pyflakes src tests

clean:  ## Remove caches and build artefacts
	find . -name __pycache__ -type d -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache build dist src/*.egg-info

distclean: clean  ## Also remove the virtual environment
	rm -rf $(VENV)
