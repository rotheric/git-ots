SHELL := /bin/bash
UV ?= uv
SOURCES := src tests
TOOL := git-ots
# The OpenTimestamps client `git-ots run` shells out to. Pinned to the version
# the integration job tests against; override to track a different release.
OTS ?= ots
OTS_CLIENT ?= opentimestamps-client==0.7.2

.DEFAULT_GOAL := help
.PHONY: help sync lint format format-check test test-integration check mutation mutation-browse build install install-dev uninstall install-client verify-install check-shadowing where prereqs run dry-run clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## --- development ---------------------------------------------------------

sync: ## Create/refresh the project virtualenv (.venv) with dev dependencies
	$(UV) sync

lint: ## Run ruff checks
	$(UV) run ruff check $(SOURCES)

format: ## Apply ruff formatting
	$(UV) run ruff format $(SOURCES)

format-check: ## Check ruff formatting without changing files
	$(UV) run ruff format --check $(SOURCES)

test: ## Run the offline unit test suite
	$(UV) run pytest

test-integration: ## Run tests that need network access and the real ots CLI
	$(UV) run pytest -m integration

check: lint format-check test ## Run the full local quality gate

mutation: ## Run mutation testing over the modules listed in [tool.mutmut]
	@# mutmut copies the tree into mutants/ and runs each mutant against the
	@# tests that cover it. mutation/sitecustomize.py is not part of the
	@# package -- it goes into the sandbox ahead of the run purely so the tests
	@# that spawn `python -m git_ots` from a throwaway working directory can
	@# still import the instrumented module (mutmut loads its own config at
	@# import time, relative to the current directory, and fails without it).
	@mkdir -p mutants/src
	@cp mutation/sitecustomize.py mutants/src/sitecustomize.py
	$(UV) run mutmut run
	$(UV) run mutmut results

mutation-browse: ## Open the interactive browser over the last mutation run
	$(UV) run mutmut browse

## --- packaging and installation -----------------------------------------

build: ## Build the wheel and sdist into dist/
	$(UV) build

install: ## Install git-ots and the OpenTimestamps client it needs
	$(UV) tool install --force --reinstall .
	@$(MAKE) --no-print-directory install-client
	@$(MAKE) --no-print-directory verify-install

install-client: ## Install the OpenTimestamps client unless it is already present
	@if command -v $(OTS) >/dev/null 2>&1; then \
		echo "OpenTimestamps client: $$(command -v $(OTS)) ($$($(OTS) --version 2>&1 | head -1))"; \
	else \
		echo "Installing OpenTimestamps client $(OTS_CLIENT) ..."; \
		$(UV) tool install "$(OTS_CLIENT)"; \
	fi

install-dev: ## Install git-ots user-wide in editable mode (tracks this working tree)
	$(UV) tool install --force --reinstall --editable .
	@$(MAKE) --no-print-directory verify-install

uninstall: ## Remove the user-wide git-ots installation
	$(UV) tool uninstall $(TOOL)

verify-install: ## Check the installed copy matches this tree and is not shadowed
	@site="$$(echo "$$($(UV) tool dir 2>/dev/null)/$(TOOL)"/lib/python*/site-packages/git_ots)"; \
	if [ -d "$$site" ]; then \
		if diff -r -q --exclude=__pycache__ src/git_ots "$$site" >/dev/null 2>&1; then \
			echo "Installed copy matches src/git_ots."; \
		else \
			echo "WARNING: the installed copy does NOT match src/git_ots."; \
			echo "uv served a cached build instead of rebuilding. Differences:"; \
			diff -r -q --exclude=__pycache__ src/git_ots "$$site" 2>&1 | sed 's/^/  /'; \
			echo; \
			echo "Retry with:  $(UV) cache clean git-ots && $(MAKE) install"; \
		fi; \
	fi
	@$(MAKE) --no-print-directory check-shadowing

check-shadowing: ## Warn if another git-ots on PATH hides the installed one
	@installed="$$($(UV) tool dir 2>/dev/null)/$(TOOL)/bin/$(TOOL)"; \
	resolved="$$(command -v $(TOOL) 2>/dev/null)"; \
	echo; \
	if [ -z "$$resolved" ]; then \
		echo "Installed, but '$(TOOL)' is not on PATH."; \
		echo "Run: $(UV) tool update-shell   (then restart your shell)"; \
	elif [ "$$(readlink -f "$$resolved" 2>/dev/null || echo "$$resolved")" = \
	      "$$(readlink -f "$$installed" 2>/dev/null || echo "$$installed")" ]; then \
		echo "Installed: $$resolved"; \
	else \
		echo "WARNING: '$(TOOL)' on PATH is NOT the copy just installed."; \
		echo "  resolves to: $$resolved"; \
		echo "  installed:   $$installed"; \
		echo; \
		echo "Another copy earlier on PATH is hiding it -- most often this"; \
		echo "project's .venv/bin, which uv sync creates and which any activated"; \
		echo "venv or 'uv run' puts first. 'make install' cannot replace that one."; \
		echo "Deactivate the venv, or run 'hash -r' if your shell cached the path."; \
	fi

where: ## Show every git-ots on PATH and flag shadowing
	@found=0; seen=""; \
	IFS=:; for dir in $$PATH; do \
		[ -x "$$dir/$(TOOL)" ] || continue; \
		real="$$(readlink -f "$$dir/$(TOOL)" 2>/dev/null || echo "$$dir/$(TOOL)")"; \
		case ":$$seen:" in *":$$real:"*) continue;; esac; \
		seen="$$seen:$$real"; \
		found=$$((found + 1)); \
		if [ $$found -eq 1 ]; then echo "$$dir/$(TOOL)   <- wins"; \
		else echo "$$dir/$(TOOL)   (shadowed)"; fi; \
	done; \
	if [ $$found -eq 0 ]; then echo "$(TOOL) is not on PATH (try 'make install')"; \
	elif [ $$found -gt 1 ]; then \
		echo; \
		echo "$$found copies found. 'make install' only replaces the uv tool copy,"; \
		echo "so a shadowing copy keeps winning until it is removed or PATH changes."; \
	fi

## --- running against this repository ------------------------------------

prereqs: ## Check everything a real `git-ots run` needs
	@ok=1; \
	if command -v $(UV) >/dev/null 2>&1; then echo "  uv          $$($(UV) --version)"; \
	else echo "  uv          MISSING"; ok=0; fi; \
	if command -v git >/dev/null 2>&1; then echo "  git         $$(git --version | cut -d' ' -f3)"; \
	else echo "  git         MISSING"; ok=0; fi; \
	if command -v $(TOOL) >/dev/null 2>&1; then echo "  $(TOOL)     $$(command -v $(TOOL))"; \
	else echo "  $(TOOL)     not installed (run 'make install')"; ok=0; fi; \
	ots="$$(git config --get ots.command 2>/dev/null)"; \
	if [ -n "$$ots" ]; then configured=1; else ots=ots; configured=0; fi; \
	if command -v "$$ots" >/dev/null 2>&1; then echo "  $$ots         $$(command -v "$$ots")"; \
	else \
		echo "  $$ots         MISSING -- 'git-ots run' cannot submit"; \
		echo "                install with: $(UV) tool install opentimestamps-client"; \
		ok=0; \
	fi; \
	if [ "$$configured" = 1 ]; then echo "  config      ots.command = $$ots (git config)"; \
	else echo "  config      none set -- built-in defaults apply"; fi; \
	echo; \
	if [ $$ok -eq 1 ]; then echo "All prerequisites satisfied."; \
	else echo "Prerequisites missing -- see above."; exit 1; fi

run: prereqs ## Check prerequisites, then timestamp this repository
	$(TOOL) run

dry-run: ## Show what a run would do (needs no OpenTimestamps client)
	$(TOOL) run --dry-run

clean: ## Remove build artifacts and tool caches
	rm -rf dist build .pytest_cache .ruff_cache mutants
	find $(SOURCES) -name '__pycache__' -type d -prune -exec rm -rf {} +
